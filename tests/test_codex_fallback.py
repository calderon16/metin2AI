"""Gemini kotası sonrası Codex yönlendirmesi; gerçek modele bağlanmaz."""

import json
import subprocess

import pytest

from qa.planner.llm import LLMError, LLMReply, Message, QuotaExhausted, ToolCall, ToolSpec
from qa.planner.codex_fallback import CodexCliProvider, QuotaFallbackProvider
from qa.planner.budget import BudgetedProvider, LLMBudget


class Primary:
    name = "gemini"
    model = "test-gemini"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools):
        self.calls += 1
        raise QuotaExhausted("günlük kota")


class Secondary:
    name = "codex_cli"
    model = "configured"

    def __init__(self):
        self.calls = 0

    def chat(self, system, messages, tools):
        self.calls += 1
        return LLMReply(Message("assistant", tool_calls=[ToolCall("observe", {})]))


def test_quota_fallback_stays_on_codex():
    primary, secondary = Primary(), Secondary()
    provider = QuotaFallbackProvider(primary, secondary)
    for _ in range(2):
        reply = provider.chat("system", [Message("user", "test")], [])
        assert reply.message.tool_calls[0].name == "observe"
        assert provider.name == "codex_cli"
    assert primary.calls == 1 and secondary.calls == 2


def test_other_llm_error_does_not_switch():
    primary, secondary = Primary(), Secondary()
    primary.chat = lambda *args: (_ for _ in ()).throw(LLMError("geçici hata"))
    provider = QuotaFallbackProvider(primary, secondary)
    with pytest.raises(LLMError, match="geçici"):
        provider.chat("system", [], [])
    assert provider.name == "gemini" and secondary.calls == 0


def test_budget_records_fallback_as_codex(service):
    service.cfg.explorer.requests_per_minute = 0
    provider = BudgetedProvider(QuotaFallbackProvider(Primary(), Secondary()),
                                LLMBudget(service.store, service.cfg.explorer), "run-test")
    provider.chat("system", [], [])
    assert service.store.query_one("SELECT provider FROM llm_usage WHERE run_id=?", ("run-test",))["provider"] == "codex_cli"


def test_codex_cli_uses_read_only_isolated_process_and_validates_tools(monkeypatch):
    seen = {}

    def fake_run(command, *, input, text, capture_output, timeout, cwd, env, check):
        seen.update(command=command, input=input, cwd=cwd, env=env)
        output = command[command.index("-o") + 1]
        with open(output, "w", encoding="utf-8") as f:
            json.dump({"text": "", "tool_calls": [{"name": "observe", "args_json": "{}"}]}, f)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("qa.planner.codex_fallback.subprocess.run", fake_run)
    monkeypatch.setenv("GEMINI_API_KEY", "never-pass-to-cli")
    provider = CodexCliProvider(executable="codex")
    reply = provider.chat("system", [Message("user", "hello")],
                          [ToolSpec("observe", "look", {"type": "object", "properties": {}})])
    assert reply.message.tool_calls[0].name == "observe"
    assert "--sandbox" in seen["command"] and "read-only" in seen["command"]
    assert "--ephemeral" in seen["command"] and "--ignore-user-config" in seen["command"]
    assert "GEMINI_API_KEY" not in seen["env"]


def test_codex_cli_rejects_unlisted_tool(monkeypatch):
    def fake_run(command, **kwargs):
        with open(command[command.index("-o") + 1], "w", encoding="utf-8") as f:
            json.dump({"text": "", "tool_calls": [{"name": "trade_with", "args_json": "{}"}]}, f)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("qa.planner.codex_fallback.subprocess.run", fake_run)
    with pytest.raises(Exception, match="izin verilmeyen"):
        CodexCliProvider(executable="codex").chat("system", [],
            [ToolSpec("observe", "look", {"type": "object", "properties": {}})])
