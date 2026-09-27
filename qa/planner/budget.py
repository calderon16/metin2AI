"""LLM bütçe koruması ve maliyet sayacı.

Her model çağrısı SQLite'a yazılır (istek, girdi/çıktı/önbellek token'ı, maliyet). Çağrıdan önce günlük
istek/token tavanı ve (ücretli kullanımda) aylık $ tavanı kontrol edilir; aşılırsa BudgetExceeded
fırlar ve keşif "bütçe doldu" diye düzgünce durur — sürpriz fatura olmaz. Ayrıca istekler arasında
hız sınırı (RPM) uygulanır; ücretsiz katmanın dakikalık sınırına takılmamak için.

Ücretsiz katmanda maliyet $0 kaydedilir; bilgi için "ücretli olsaydı" maliyeti (list_cost_usd) de tutulur.

Kullanım ve tavanlar sağlayıcıya göre ayrıdır: her kotalı sağlayıcı (gemini, codex_cli…) günlük tavanı kendi
kullanımıyla doldurur. Yerel sağlayıcılar (ollama) kaydedilir ama hiçbir tavana sayılmaz. Gemini tavanları
[explorer] provider "ollama" olsa bile uygulanır (ör. öğretmen çağrıları).

Paralel çağrılar: kilit yalnız "kontrol + yer ayır" ve "kaydet + iade" sırasında tutulur; model çağrısı
kilitsiz yapılır. Süren çağrılar rezervasyon olarak sayıldığı için günlük istek tavanı yine aşılmaz.
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
_BUDGET_LOCK = threading.Lock()
_LAST_CALL: dict[str, float] = {}
# Sürmekte olan (henüz kaydedilmemiş) çağrılar: sağlayıcı -> adet. Yalnız _BUDGET_LOCK altında değişir.
_RESERVED: dict[str, int] = {}

# Kota ve ücret olmayan yerel sağlayıcılar: kullanım kaydedilir, tavanlara sayılmaz
LOCAL_PROVIDERS = frozenset({"ollama"})


def is_metered(provider: str | None) -> bool:
    return bool(provider) and provider not in LOCAL_PROVIDERS


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
    def _sum(self, col: str, key: str, provider: str | None = None) -> dict[str, Any]:
        where, args = f"{col}=?", [key]
        if provider is not None:
            where += " AND provider=?"
            args.append(provider)
        r = self.store.query_one(
            f"SELECT COALESCE(SUM(requests),0) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens,"
            f" COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
            f" COALESCE(SUM(cost_usd),0) AS cost_usd, COALESCE(SUM(list_cost_usd),0) AS list_cost_usd"
            f" FROM llm_usage WHERE {where}", tuple(args)) or {}
        r["total_tokens"] = r.get("input_tokens", 0) + r.get("output_tokens", 0)
        r["cost_usd"] = round(r.get("cost_usd", 0.0), 4)
        r["list_cost_usd"] = round(r.get("list_cost_usd", 0.0), 4)
        return r

    def today(self, provider: str | None = None) -> dict[str, Any]:
        return self._sum("day", _now().strftime("%Y-%m-%d"), provider)

    def month(self, provider: str | None = None) -> dict[str, Any]:
        return self._sum("month", _now().strftime("%Y-%m"), provider)

    def providers(self) -> list[str]:
        """Yapılandırılmış sağlayıcılar ve bu ay kullanılanlar (sıralı, tekrarsız)."""
        rows = self.store.query("SELECT DISTINCT provider FROM llm_usage WHERE month=? AND provider IS NOT NULL",
                                (_now().strftime("%Y-%m"),))
        names = [getattr(self.cfg, "provider", None), getattr(self.cfg, "fallback_provider", None)]
        names += [r["provider"] for r in rows]
        return list(dict.fromkeys(n for n in names if n))

    def daily(self, days: int = 30) -> list[dict[str, Any]]:
        return self.store.query(
            "SELECT day, SUM(requests) AS requests, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cached_tokens) AS cached_tokens,"
            " ROUND(SUM(cost_usd), 4) AS cost_usd, ROUND(SUM(list_cost_usd), 4) AS list_cost_usd"
            " FROM llm_usage GROUP BY day ORDER BY day DESC LIMIT ?", (int(days),))

    def blocked_reason(self, provider: str | None = None, reserved: int = 0) -> str | None:
        """Sağlayıcının bütçesi dolduysa nedeni (Türkçe), değilse None. provider verilmezse [explorer] provider.
        Yerel sağlayıcılar (ollama) sınırlanmaz; diğerleri tavanı kendi kullanımıyla doldurur."""
        c = self.cfg
        provider = provider or getattr(c, "provider", "") or ""
        if not is_metered(provider):
            return None
        t = self.today(provider)
        tag = f" [{provider}]"
        if c.daily_request_limit is not None and t["requests"] + reserved >= c.daily_request_limit:
            return f"Günlük LLM istek tavanı doldu ({t['requests'] + reserved}/{c.daily_request_limit}){tag}"
        if c.daily_token_limit is not None and t["total_tokens"] >= c.daily_token_limit:
            return f"Günlük LLM token tavanı doldu ({t['total_tokens']}/{c.daily_token_limit}){tag}"
        if not c.free_tier and c.monthly_cost_limit_usd is not None:
            m = self.month()                # harcama tavanı ücretli sağlayıcıların toplamıdır
            if m["cost_usd"] >= c.monthly_cost_limit_usd:
                return f"Aylık LLM harcama tavanı doldu (${m['cost_usd']:.2f}/${c.monthly_cost_limit_usd:.2f})"
        return None

    def check(self, provider: str | None = None) -> None:
        reason = self.blocked_reason(provider)
        if reason:
            raise BudgetExceeded(reason)

    # ------------------------------------------------------------------ rezervasyon
    def reserve(self, provider: str) -> None:
        """Kilit altında: tavanı (süren çağrılar dahil) kontrol et ve bir istek yeri ayır."""
        with _BUDGET_LOCK:
            if is_metered(provider):
                reason = self.blocked_reason(provider, _RESERVED.get(provider, 0))
                if reason:
                    raise BudgetExceeded(reason)
            _RESERVED[provider] = _RESERVED.get(provider, 0) + 1

    def settle(self, reserved_as: str, provider: str | None, model: str, run_id: str | None,
               usage: dict[str, int] | None) -> None:
        """Kilit altında: ayrılan yeri iade et; çağrı başarılıysa (usage verildiyse) kullanımı kaydet."""
        with _BUDGET_LOCK:
            _RESERVED[reserved_as] = max(0, _RESERVED.get(reserved_as, 0) - 1)
            if usage is not None:
                self.record(provider or reserved_as, model, run_id, usage)

    def status(self) -> dict[str, Any]:
        c = self.cfg
        t, m = self.today(), self.month()
        main = getattr(c, "provider", "") or ""
        by_provider = {}
        for name in self.providers():
            pt = self.today(name)
            by_provider[name] = {
                "metered": is_metered(name), "today": pt, "month": self.month(name),
                "remaining_requests_today": None if not is_metered(name) or c.daily_request_limit is None
                else max(0, c.daily_request_limit - pt["requests"]),
                "blocked": self.blocked_reason(name)}
        mt = self.today(main)
        return {
            "free_tier": c.free_tier,
            "provider": main,
            "today": t,
            "month": m,
            "providers": by_provider,
            "limits": {"daily_requests": c.daily_request_limit, "daily_tokens": c.daily_token_limit,
                       "monthly_cost_usd": None if c.free_tier else c.monthly_cost_limit_usd,
                       "requests_per_minute": c.requests_per_minute,
                       "applies_to": "sağlayıcı başına (ollama hariç)"},
            "remaining_requests_today": None if c.daily_request_limit is None or not is_metered(main)
            else max(0, c.daily_request_limit - mt["requests"]),
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

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return self.inner.model

    def _rate_limit(self) -> None:
        rpm = self.budget.cfg.requests_per_minute
        if not rpm or rpm <= 0 or self.name == "ollama":
            return
        interval = 60.0 / rpm
        key = f"{self.name}:{self.model}"
        with _RATE_LOCK:
            wait = _LAST_CALL.get(key, 0.0) + interval - time.monotonic()
            _LAST_CALL[key] = time.monotonic() + max(0.0, wait)
        if wait > 0:
            time.sleep(wait)

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMReply:
        # Kilit yalnız yer ayırma ve kayıt sırasında tutulur; model çağrısı kilitsiz (paralel ajanlar beklemez)
        reserved_as = self.name
        self.budget.reserve(reserved_as)
        usage = None
        try:
            self._rate_limit()
            reply = self.inner.chat(system, messages, tools)
            usage = reply.usage or {}
            return reply
        finally:
            # Yedeğe geçiş (QuotaFallbackProvider) çağrı sırasında olabilir: kayıt asıl kullanılan sağlayıcıya
            self.budget.settle(reserved_as, self.name, self.model, self.run_id, usage)
