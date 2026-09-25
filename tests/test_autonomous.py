import io
import json
import urllib.error

import pytest

from qa.planner.autonomous import AutoExplorer, ExploreBudget, behaviour_tools, meta_tools
from qa.planner.llm import GeminiProvider, LLMError, Message, ScriptedProvider, ToolResult, ToolSpec, make_provider

SHOP_SCRIPT = [
    {"name": "qa_setup", "args": {"ops": "- set_gold: 30"}},
    {"name": "observe", "args": {}},
    [{"name": "talk_npc", "args": {"vnum": 9001}},
     {"name": "buy_item", "args": {"vnum": 27001, "expect_error": "NOT_ENOUGH_GOLD"}}],
    {"name": "check", "args": {"asserts": "- gold: {min: 0}\n- inventory_not_contains: 27001"}},
    {"name": "talk_npc", "args": {"vnum": 424242}},                  # LLM hatası: olmayan NPC
    {"name": "report_finding", "args": {"title": "Yetersiz yangla alım", "severity": "bug",
                                        "description": "Yang 30 iken 50'lik iksir alınabildi", "step": 2}},
    {"name": "finish", "args": {"summary": "Dükkan yetersiz yang edge-case'i test edildi"}},
]


def _explorer(service, script):
    return AutoExplorer(service.cfg, service.store, ScriptedProvider(script), service.factory)


def test_tool_schemas():
    tools = {t.name: t for t in behaviour_tools() + meta_tools()}
    assert {"kill_monster", "buy_item", "check", "finish", "report_finding", "qa_setup"} <= set(tools)
    assert "trade_with" not in tools
    km = tools["kill_monster"].parameters
    assert km["properties"]["vnum"]["type"] == "integer" and km["required"] == ["vnum"]
    assert "auto_potion" not in km["properties"]
    assert {"wait_for_event", "screenshot", "trade_with", "party_invite"}.isdisjoint(tools)
    assert all("expect_error" in t.parameters["properties"] for t in behaviour_tools())
    assert all(n.replace("_", "").isalnum() for n in tools)


def test_healthy_run_saves_scenario_that_passes(service):
    out = _explorer(service, SHOP_SCRIPT).run("Dükkanı test et", seed=1, save_as_scenario="auto_shop",
                                              budget=ExploreBudget(max_steps=10))
    assert out.stop_reason == "finished" and out.steps == 3
    assert out.agent_summary.startswith("Dükkan")
    # Bulgu "bug" olarak kaydedildi ama sağlıklı oyunda assert'ler geçti; LLM'in yanlış NPC adımı action_error
    names = {f["name"] for f in service.get_test_result(out.run_id)["failures"]}
    assert names == {"NOT_FOUND", "Yetersiz yangla alım"}
    sc = service.get_scenario("auto_shop")
    assert "424242" not in sc and "expect_error: NOT_ENOUGH_GOLD" in sc and "set_gold: 30" in sc
    assert out.validation["result"] == "PASSED"
    rep = service.get_test_result(out.run_id)
    assert rep["llm"]["provider"] == "scripted" and rep["stop_reason"] == "finished"
    assert (service.cfg.artifacts_path / out.run_id / "llm_transcript.jsonl").exists()


def test_bug_run_produces_failing_regression(service):
    service.cfg.sim.faults = ["negative_gold_on_buy"]
    out = _explorer(service, SHOP_SCRIPT).run("Dükkanı test et", seed=1, save_as_scenario="auto_shop_bug")
    kinds = {(f["kind"], f["name"]) for f in service.get_test_result(out.run_id)["failures"]}
    assert ("assertion", "expect_error") in kinds and ("assertion", "gold") in kinds
    # Hatayı gösteren adım senaryoda kaldı → regression senaryosu bug düzelene kadar FAILED
    assert out.validation["result"] == "FAILED"
    service.cfg.sim.faults = []
    assert service.run_scenario("auto_shop_bug", seed=1)["result"] == "PASSED"   # "bug düzeltildi"


def test_setup_only_before_first_step(service):
    script = [{"name": "wait", "args": {"ms": 100}},
              {"name": "qa_setup", "args": {"ops": "- set_gold: 99999"}},
              {"name": "finish", "args": {"summary": "x"}}]
    prov = ScriptedProvider(script)
    AutoExplorer(service.cfg, service.store, prov, service.factory).run("x", seed=1)
    last_tool_msg = prov.seen[-1][-1]
    assert "ilk oyuncu adımından önce" in last_tool_msg.tool_results[0].content["error"]


def test_budget_and_bad_calls(service):
    script = [[{"name": "wait", "args": {"ms": 50}}, {"name": "rm_rf", "args": {}}]] * 10
    prov = ScriptedProvider(script)
    out = AutoExplorer(service.cfg, service.store, prov, service.factory).run(
        "x", seed=1, budget=ExploreBudget(max_steps=3, max_turns=8))
    assert out.steps == 3 and out.stop_reason in {"step_budget", "max_turns"}
    results = [r.content for m in prov.seen[-1] if m.role == "tool" for r in m.tool_results]
    assert any("Bilinmeyen tool" in str(r.get("error")) for r in results)
    assert any("bütçesi doldu" in str(r.get("error")) for r in results)


def test_idle_model_stops(service):
    out = AutoExplorer(service.cfg, service.store, ScriptedProvider([]), service.factory).run(
        "x", seed=1, budget=ExploreBudget(max_idle_turns=1))
    assert out.stop_reason == "no_tool_calls" and out.steps == 0


def test_history_trim_keeps_pairs():
    h = [Message("user", "goal")]
    for i in range(20):
        h += [Message("assistant", str(i)), Message("tool", tool_results=[ToolResult("x", {})])]
    t = AutoExplorer._trim(h, 5)
    assert t[0].text == "goal" and len(t) == 11 and t[1].role == "assistant" and t[1].text == "15"


# ---------------------------------------------------------------------- Gemini REST

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gemini_reply(parts, usage=None):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": usage or {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}


def test_gemini_request_and_parse(monkeypatch):
    sent = []

    def opener(req, timeout):
        sent.append((req.full_url, dict(req.header_items()), json.loads(req.data)))
        return _Resp(json.dumps(_gemini_reply([
            {"text": "Önce dükkana gideyim."},
            {"functionCall": {"name": "talk_npc", "args": {"vnum": 9001}}, "thoughtSignature": "sig=="},
        ])).encode())

    p = GeminiProvider("gemini-test", api_key="k", opener=opener)
    tools = [ToolSpec("talk_npc", "NPC", {"type": "object", "properties": {"vnum": {"type": "integer"}},
                                          "required": ["vnum"], "additionalProperties": False})]
    r = p.chat("sys", [Message("user", "hedef")], tools)
    url, headers, body = sent[0]
    assert url.endswith("/models/gemini-test:generateContent")
    assert headers["X-goog-api-key"] == "k"
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    fd = body["tools"][0]["functionDeclarations"][0]
    assert fd["name"] == "talk_npc" and "additionalProperties" not in fd["parameters"]
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    assert r.message.tool_calls[0].name == "talk_npc" and r.message.tool_calls[0].args == {"vnum": 9001}
    assert r.usage["total_tokens"] == 15 and r.message.text == "Önce dükkana gideyim."

    # İkinci tur: modelin içeriği (thoughtSignature dahil) aynen, tool sonucu functionResponse olarak gider
    p.chat("sys", [Message("user", "hedef"), r.message,
                   Message("tool", text="not", tool_results=[ToolResult("talk_npc", {"ok": True})])], tools)
    contents = sent[1][2]["contents"]
    assert contents[1]["parts"][1]["thoughtSignature"] == "sig=="
    assert contents[2] == {"role": "user", "parts": [
        {"functionResponse": {"name": "talk_npc", "response": {"ok": True}}}, {"text": "not"}]}


def test_gemini_retries_then_fails(monkeypatch):
    calls = []

    def opener(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 429, "quota", {}, io.BytesIO(b'{"error":"quota"}'))

    monkeypatch.setattr("time.sleep", lambda s: None)
    p = GeminiProvider("m", api_key="k", opener=opener, max_retries=2)
    with pytest.raises(LLMError, match="kotası aşıldı"):
        p.chat("s", [Message("user", "x")], [])
    assert len(calls) == 3


def test_gemini_needs_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        make_provider("gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom")
    assert make_provider("gemini").model == "gemini-custom"


def test_llm_error_marks_run_error(service):
    class Broken:
        name, model = "broken", "b"

        def chat(self, *a):
            raise LLMError("HTTP 500")

    out = AutoExplorer(service.cfg, service.store, Broken(), service.factory).run("x", seed=1)
    assert out.stop_reason == "llm_error" and out.result == "ERROR"


def test_gemini_over_real_http(service, monkeypatch):
    """Gerçek HTTP yolu: yerel sahte Gemini sunucusu + GeminiProvider + otonom döngü."""
    import http.server
    import threading

    replies = [
        [{"functionCall": {"name": "qa_setup", "args": {"ops": "- set_gold: 1000"}}}],
        [{"functionCall": {"name": "buy_item", "args": {"vnum": 27001, "npc_vnum": 9001, "count": 2}}}],
        [{"functionCall": {"name": "check", "args": {"asserts": "- gold: {delta: -100}"}}}],
        [{"functionCall": {"name": "finish", "args": {"summary": "tamam"}}}],
    ]
    bodies = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append((self.path, self.headers.get("x-goog-api-key"),
                           json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            data = json.dumps(_gemini_reply(replies[len(bodies) - 1])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    try:
        prov = GeminiProvider("gemini-fake", api_key="secret",
                              base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1beta")
        out = service.explore_auto("Dükkan", provider=prov, save_as_scenario="auto_http", seed=2)
    finally:
        srv.shutdown()
    assert out["stop_reason"] == "finished" and out["result"] == "PASSED" and out["usage"]["total_tokens"] == 60
    assert bodies[0][0] == "/v1beta/models/gemini-fake:generateContent" and bodies[0][1] == "secret"
    # 2. istekte 1. turun functionCall'ı ve sonucu geçmişte
    assert bodies[1][2]["contents"][1]["parts"][0]["functionCall"]["name"] == "qa_setup"
    assert "functionResponse" in bodies[1][2]["contents"][2]["parts"][0]
    assert out["validation"]["result"] == "PASSED"
