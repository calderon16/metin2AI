"""LLM maliyeti ve bütçe koruması; ağ çağrısı yapmaz."""

import io
import json

import pytest

from qa.daemon.core import Daemon
from qa.planner.autonomous import AutoExplorer, ExploreBudget, _observe_view, _step_view
from qa.planner.budget import BudgetedProvider, LLMBudget
from qa.planner.llm import GeminiProvider, Message, ScriptedProvider


USAGE = {"input_tokens": 1000, "cached_tokens": 200, "output_tokens": 100, "total_tokens": 1100}


def test_price_record_and_limits(service):
    cfg = service.cfg.explorer
    b = LLMBudget(service.store, cfg)
    expected = (800 * .10 + 200 * .025 + 100 * .40) / 1_000_000
    assert b.price(USAGE) == pytest.approx(expected)
    assert b.record("scripted", "test", "run-1", USAGE) == 0
    assert b.today()["cost_usd"] == 0
    assert b.today()["list_cost_usd"] > 0
    cfg.daily_request_limit = 1
    assert "istek" in b.blocked_reason()
    cfg.daily_request_limit = None
    cfg.daily_token_limit = 1100
    assert "token" in b.blocked_reason()
    cfg.daily_token_limit = None
    cfg.monthly_cost_limit_usd = .00001
    assert b.blocked_reason() is None  # ücretsiz katman harcama tavanını tüketmez
    cfg.free_tier = False
    b.record("scripted", "test", "run-2", USAGE)
    assert b.today()["cost_usd"] > 0
    assert "harcama" in b.blocked_reason()


def test_budgeted_provider_rate_and_usage(service, monkeypatch):
    import qa.planner.budget as mod

    clock = [1000.0]
    waits = []
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mod.time, "sleep", lambda s: (waits.append(s), clock.__setitem__(0, clock[0] + s)))
    mod._LAST_CALL.clear()
    service.cfg.explorer.requests_per_minute = 600
    inner = ScriptedProvider([{"name": "finish", "args": {"summary": "ok"}}] * 2)
    p = BudgetedProvider(inner, LLMBudget(service.store, service.cfg.explorer), "r1")
    for _ in range(2):
        p.chat("s", [Message("user", "x")], [])
    assert waits == pytest.approx([.1])
    assert p.budget.today()["requests"] == 2
    assert len(service.store.query("SELECT run_id FROM llm_usage")) == 2


def test_explore_stops_cleanly_at_daily_limit(service):
    service.cfg.explorer.daily_request_limit = 1
    script = [{"name": "observe", "args": {}}, {"name": "finish", "args": {"summary": "ok"}}]
    out = AutoExplorer(service.cfg, service.store, ScriptedProvider(script), service.factory).run(
        "Test et", seed=3, budget=ExploreBudget(max_steps=3))
    assert out.stop_reason == "budget_exhausted"
    assert out.result != "ERROR"
    assert LLMBudget(service.store, service.cfg.explorer).today()["requests"] == 1


def test_job_validation_and_campaign_budget_skip(cfg):
    cfg.explorer.daily_request_limit = 0
    d = Daemon(cfg, llm_factory=lambda: ScriptedProvider([]))
    with pytest.raises(ValueError, match="istek tavanı"):
        d.jobs.validate("explore", {"goal": "test"})
    d.start()
    try:
        assert d.agents.wait_online(4, 10)
        job = d.jobs.wait(d.jobs.submit("campaign", {"systems": ["npc_shop"], "explore": True, "seed": 2}), 60)
        assert job["status"] == "done", job.get("error")
        matrix = d.db.get_campaign(job["result"]["campaign_id"])["matrix"]["npc_shop"]
        assert "istek tavanı" in matrix["explore"]["skipped"]
        assert matrix["status"] != "error"
    finally:
        d.shutdown()


def test_usage_api_and_cli(service, cfg, capsys, monkeypatch):
    from qa.daemon.server import Api
    from qa.cli import main

    LLMBudget(service.store, cfg.explorer).record("scripted", "test", "r", USAGE)
    d = Daemon(cfg, llm_factory=lambda: ScriptedProvider([]))
    assert d.service.llm_usage()["today"]["requests"] == 1
    assert Api(d).dispatch("GET", "/api/llm-usage", {}, None)["today"]["requests"] == 1
    monkeypatch.setattr("qa.service.QaService", lambda unused_cfg: service)
    assert main(["--config", "qa.toml", "llm-usage"]) == 0
    assert "1 istek" in capsys.readouterr().out


def test_gemini_usage_and_thinking_budget():
    sent = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(req, timeout):
        sent.append(json.loads(req.data))
        return Resp(json.dumps({"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]} }],
                                "usageMetadata": {"promptTokenCount": 100, "cachedContentTokenCount": 40,
                                                  "candidatesTokenCount": 10, "thoughtsTokenCount": 5,
                                                  "totalTokenCount": 115}}).encode())

    p = GeminiProvider("gemini-test", api_key="test", opener=opener, thinking_budget=32)
    reply = p.chat("s", [Message("user", "x")], [])
    assert sent[0]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 32}
    assert reply.usage == {"input_tokens": 100, "cached_tokens": 40, "output_tokens": 15, "total_tokens": 115}


def test_compact_views():
    o = _observe_view({"state": {"hp": 10, "gold": 5, "unused": "x"},
                       "inventory": {"items": [{"vnum": 10, "count": 2, "slot": 0}]},
                       "nearby": [{"vid": 1, "type": "mob", "vnum": 101, "name": "Köpek", "distance": 4}]})
    assert o["inventory"] == ["10x2@0"] and "unused" not in o["state"]
    r = _step_view({"step": 1, "status": "ok", "client_events": [{"event": "damage_dealt", "data": {}}] * 20,
                    "state": {"hp": 9, "secret": "x"}, "windows": {"shop": {}}})
    assert r["counts"]["damage_dealt"] == 20 and r["windows"] == ["shop"]
    assert len(json.dumps(r)) < 500
