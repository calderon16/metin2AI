"""LLM bütçe koruması ve maliyet sayacı.

Her model çağrısı SQLite'a yazılır (istek, girdi/çıktı/önbellek token'ı, maliyet). Çağrıdan önce günlük
istek/token tavanı ve (ücretli kullanımda) aylık $ tavanı kontrol edilir; aşılırsa BudgetExceeded
fırlar ve keşif "bütçe doldu" diye düzgünce durur — sürpriz fatura olmaz. Ayrıca istekler arasında
hız sınırı (RPM) uygulanır; ücretsiz katmanın dakikalık sınırına takılmamak için.

Ücretsiz katmanda maliyet $0 kaydedilir; bilgi için "ücretli olsaydı" maliyeti (list_cost_usd) de tutulur.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from ..config import ExplorerConfig
from ..store.db import Store
from .llm import LLMError, LLMProvider, LLMReply, Message, ToolSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    month TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    run_id TEXT,
    requests INTEGER NOT NULL DEFAULT 1,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    list_cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS llm_usage_day ON llm_usage(day);
CREATE INDEX IF NOT EXISTS llm_usage_month ON llm_usage(month);
"""

# Aynı süreçteki tüm keşifler (paralel daemon işleri dahil) tek hız sınırını paylaşır
_RATE_LOCK = threading.Lock()
_LAST_CALL: dict[str, float] = {}


class BudgetExceeded(LLMError):
    """Günlük/aylık LLM bütçesi doldu — hata değil, planlı durma."""


def _now() -> datetime:
    # Günlük kota Google tarafında Pasifik saatiyle sıfırlanır; basitlik için UTC günü kullanılır
    return datetime.now(timezone.utc)


class LLMBudget:
    def __init__(self, store: Store, cfg: ExplorerConfig):
        self.store = store
        self.cfg = cfg
        store.executescript(SCHEMA)

    # ------------------------------------------------------------------ maliyet
    def price(self, usage: dict[str, int]) -> float:
        c = self.cfg
        inp = int(usage.get("input_tokens", 0))
        cached = min(int(usage.get("cached_tokens", 0)), inp)
        out = int(usage.get("output_tokens", 0))
        return ((inp - cached) * c.price_input_per_m + cached * c.price_cached_per_m
                + out * c.price_output_per_m) / 1_000_000

    def record(self, provider: str, model: str, run_id: str | None, usage: dict[str, int]) -> float:
        list_cost = self.price(usage)
        cost = 0.0 if self.cfg.free_tier else list_cost
        now = _now()
        self.store.execute(
            "INSERT INTO llm_usage (ts, day, month, provider, model, run_id, requests, input_tokens, output_tokens,"
            " cached_tokens, cost_usd, list_cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"), provider, model,
             run_id, 1, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
             int(usage.get("cached_tokens", 0)), cost, list_cost))
        return cost

    # ------------------------------------------------------------------ özetler
    def _sum(self, col: str, key: str) -> dict[str, Any]:
        r = self.store.query_one(
            f"SELECT COALESCE(SUM(requests),0) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens,"
            f" COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
            f" COALESCE(SUM(cost_usd),0) AS cost_usd, COALESCE(SUM(list_cost_usd),0) AS list_cost_usd"
            f" FROM llm_usage WHERE {col}=?", (key,)) or {}
        r["total_tokens"] = r.get("input_tokens", 0) + r.get("output_tokens", 0)
        r["cost_usd"] = round(r.get("cost_usd", 0.0), 4)
        r["list_cost_usd"] = round(r.get("list_cost_usd", 0.0), 4)
        return r

    def today(self) -> dict[str, Any]:
        return self._sum("day", _now().strftime("%Y-%m-%d"))

    def month(self) -> dict[str, Any]:
        return self._sum("month", _now().strftime("%Y-%m"))

    def daily(self, days: int = 30) -> list[dict[str, Any]]:
        return self.store.query(
            "SELECT day, SUM(requests) AS requests, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cached_tokens) AS cached_tokens,"
            " ROUND(SUM(cost_usd), 4) AS cost_usd, ROUND(SUM(list_cost_usd), 4) AS list_cost_usd"
            " FROM llm_usage GROUP BY day ORDER BY day DESC LIMIT ?", (int(days),))

    def blocked_reason(self) -> str | None:
        """Bütçe dolduysa nedeni (Türkçe), değilse None."""
        c = self.cfg
        t = self.today()
        if c.daily_request_limit is not None and t["requests"] >= c.daily_request_limit:
            return f"Günlük LLM istek tavanı doldu ({t['requests']}/{c.daily_request_limit})"
        if c.daily_token_limit is not None and t["total_tokens"] >= c.daily_token_limit:
            return f"Günlük LLM token tavanı doldu ({t['total_tokens']}/{c.daily_token_limit})"
        if not c.free_tier and c.monthly_cost_limit_usd is not None:
            m = self.month()
            if m["cost_usd"] >= c.monthly_cost_limit_usd:
                return f"Aylık LLM harcama tavanı doldu (${m['cost_usd']:.2f}/${c.monthly_cost_limit_usd:.2f})"
        return None

    def check(self) -> None:
        reason = self.blocked_reason()
        if reason:
            raise BudgetExceeded(reason)

    def status(self) -> dict[str, Any]:
        c = self.cfg
        t, m = self.today(), self.month()
        return {
            "free_tier": c.free_tier,
            "today": t,
            "month": m,
            "limits": {"daily_requests": c.daily_request_limit, "daily_tokens": c.daily_token_limit,
                       "monthly_cost_usd": None if c.free_tier else c.monthly_cost_limit_usd,
                       "requests_per_minute": c.requests_per_minute},
            "remaining_requests_today": None if c.daily_request_limit is None
            else max(0, c.daily_request_limit - t["requests"]),
            "blocked": self.blocked_reason(),
            "prices_per_m": {"input": c.price_input_per_m, "output": c.price_output_per_m,
                             "cached": c.price_cached_per_m},
        }


class BudgetedProvider:
    """Herhangi bir LLMProvider'ı bütçe kontrolü, hız sınırı ve kullanım kaydıyla sarar."""

    def __init__(self, inner: LLMProvider, budget: LLMBudget, run_id: str | None = None):
        self.inner = inner
        self.budget = budget
        self.run_id = run_id
        self.name = inner.name
        self.model = inner.model

    def _rate_limit(self) -> None:
        rpm = self.budget.cfg.requests_per_minute
        if not rpm or rpm <= 0:
            return
        interval = 60.0 / rpm
        key = f"{self.name}:{self.model}"
        with _RATE_LOCK:
            wait = _LAST_CALL.get(key, 0.0) + interval - time.monotonic()
            _LAST_CALL[key] = time.monotonic() + max(0.0, wait)
        if wait > 0:
            time.sleep(wait)

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMReply:
        self.budget.check()
        self._rate_limit()
        reply = self.inner.chat(system, messages, tools)
        self.budget.record(self.name, self.model, self.run_id, reply.usage)
        return reply
