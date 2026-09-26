"""Yerel Ollama sağlayıcısı: istek biçimi, tool çağrıları, JSON metin yedeği ve hata mesajları (sahte HTTP)."""

import io
import json
import urllib.error

import pytest

from qa.config import ExplorerConfig
from qa.planner.llm import (LLMError, Message, OllamaProvider, ToolCall, ToolResult, ToolSpec, make_provider)

TOOLS = [ToolSpec("walk_to", "Yürü", {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}}),
         ToolSpec("finish", "Bitir", {"type": "object", "properties": {"summary": {"type": "string"}}})]


def _opener(replies, sent):
    def opener(req, timeout):
        sent.append(json.loads(req.data.decode("utf-8")))
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(json.dumps(r).encode("utf-8"))
    return opener


def test_ollama_native_tool_calls_and_history():
    sent = []
    reply = {"message": {"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": "walk_to", "arguments": {"x": 1, "y": 2}}}]},
             "prompt_eval_count": 120, "eval_count": 8, "done_reason": "stop"}
    p = OllamaProvider("qwen2.5:7b", opener=_opener([reply, reply], sent), num_ctx=4096)
    r = p.chat("sistem", [Message("user", "köye git")], TOOLS)
    assert r.message.tool_calls[0].name == "walk_to" and r.message.tool_calls[0].args == {"x": 1, "y": 2}
    assert r.usage == {"input_tokens": 120, "output_tokens": 8, "cached_tokens": 0, "total_tokens": 128}
    body = sent[0]
    assert body["model"] == "qwen2.5:7b" and body["stream"] is False and body["options"]["num_ctx"] == 4096
    assert body["messages"][0] == {"role": "system", "content": "sistem"}
    assert body["tools"][0]["function"]["name"] == "walk_to"
    # ikinci tur: asistanın çağrısı ve tool sonucu geri gider
    p.chat("sistem", [Message("user", "köye git"), r.message,
                      Message("tool", tool_results=[ToolResult("walk_to", {"ok": True})])], TOOLS)
    msgs = sent[1]["messages"]
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "walk_to", "arguments": {"x": 1, "y": 2}}
    assert msgs[3] == {"role": "tool", "tool_name": "walk_to", "content": '{"ok": true}'}


def test_ollama_parses_json_tool_call_from_text():
    sent = []
    text = 'Önce yürüyorum.\n{"name": "walk_to", "arguments": {"x": 5, "y": 6}}'
    p = OllamaProvider("m", opener=_opener([{"message": {"content": text}}], sent))
    r = p.chat("s", [Message("user", "x")], TOOLS)
    assert r.message.tool_calls == [ToolCall("walk_to", {"x": 5, "y": 6})]


def test_ollama_missing_model_and_unreachable():
    missing = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b'{"error":"model \'x\' not found"}'))
    p = OllamaProvider("x", opener=_opener([missing], []))
    with pytest.raises(LLMError, match="ollama pull x"):
        p.chat("s", [Message("user", "x")], TOOLS)
    down = urllib.error.URLError("refused")
    p = OllamaProvider("x", opener=_opener([down, down], []), max_retries=1, sleep=lambda s: None)
    with pytest.raises(LLMError, match="Ollama çalışıyor mu"):
        p.chat("s", [Message("user", "x")], TOOLS)


def test_make_provider_ollama_ignores_cloud_options(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    p = make_provider("ollama", None, api_key_env="GEMINI_API_KEY", thinking_budget=0, base_url="http://h:1")
    assert isinstance(p, OllamaProvider) and p.model == "qwen2.5:7b" and p.base_url == "http://h:1"


def test_budget_does_not_limit_ollama(tmp_path):
    from qa.planner.budget import LLMBudget
    from qa.store.db import Store

    cfg = ExplorerConfig(provider="ollama", daily_request_limit=1)
    b = LLMBudget(Store(tmp_path / "q.sqlite"), cfg)
    b.record("ollama", "m", None, {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11})
    b.record("ollama", "m", None, {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11})
    assert b.blocked_reason() is None
