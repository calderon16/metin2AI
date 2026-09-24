import asyncio
import json
import sys

import pytest

from qa import mcp_server
from qa.oracle.signals import FileEventSource, LogFileSource, ServerSignals


def _call(name, args):
    out = asyncio.run(mcp_server.mcp.call_tool(name, args))
    # FastMCP yapılandırılmış çıktı varsa (içerik, yapı) çifti, yoksa yalnızca içerik listesi döner
    return out[0] if isinstance(out, tuple) else out


@pytest.fixture
def mcp_svc(service, monkeypatch):
    monkeypatch.setattr(mcp_server, "_svc", service)
    return service


def test_mcp_tools_registered():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    for n in ("build", "run_scenario", "get_failed_tests", "replay_failure", "get_screenshot",
              "explore_start", "explore_step", "explore_finish", "reset_test_account", "write_scenario"):
        assert n in names


def test_mcp_run_and_inspect(mcp_svc):
    rep = json.loads(_call("run_scenario", {"name": "smoke_login_walk", "seed": 4})[0].text)
    assert rep["result"] == "PASSED"
    rid = rep["run_id"]
    assert "walk_to" in _call("get_trace", {"run_id": rid})[0].text
    img = _call("get_screenshot", {"run_id": rid})[0]
    assert img.type == "image" and img.mimeType == "image/png"
    # Liste döndüren tool'larda FastMCP her öğeyi ayrı içerik olarak gönderir
    first = json.loads(_call("get_test_runs", {"limit": 5})[0].text)
    assert first["run_id"] == rid


def test_write_scenario_then_run(mcp_svc):
    y = "name: claude_new\nsteps:\n  - walk_to: {x: 5500, y: 5500}\nassert:\n  - alive\n"
    assert json.loads(_call("write_scenario", {"name": "claude_new", "yaml_text": y})[0].text)["ok"]
    rep = mcp_svc.run_scenario("claude_new", seed=1)
    assert rep["result"] == "PASSED"


def test_suite_and_failed_tests(service):
    service.cfg.sim.faults = ["sell_no_gold"]
    res = service.run_suite(tag="shop", seed=2)
    assert not res["ok"]
    failed = service.get_failed_tests()
    assert failed and failed[0]["failures"]
    assert "server.log" in service.get_server_logs(failed[0]["run_id"])


def test_build_whitelist(service, tmp_path):
    assert not service.build()["ok"]  # tanımsız
    service.cfg.build.commands = {"ok": [sys.executable, "-c", "print('derlendi')"],
                                  "bad": [sys.executable, "-c", "import sys; print('main.cpp:1: error: x'); sys.exit(2)"]}
    r = service.build("ok")
    assert r["ok"] and "derlendi" in r["results"][0]["output_tail"]
    r = service.build("bad")
    assert not r["ok"] and r["results"][0]["error_lines"]
    assert "Bilinmeyen" in service.build("rm -rf /")["error"]


def test_live_state_and_reset(service):
    st = service.get_player_state()
    assert st["player"]["in_game"] and st["player"]["name"] == "AI_QA_001"
    assert service.reset_test_account("AI_QA_002")["ok"]
    with pytest.raises(Exception):
        service.reset_test_account("SomeRealPlayer")


def test_exploration_flow(service):
    ex = service.explorer.start("Pazar/shop sistemini test et", setup=[{"set_gold": 60}])
    assert ex["ok"] and "shop" in ex["checklist"]["area_checks"]
    rid = ex["run_id"]
    st = service.explorer.step(rid, {"buy_item": {"vnum": 27001, "npc_vnum": 9001}})
    assert st["status"] == "passed" and st["diff"]["gold"] == [60, 10]
    st = service.explorer.step(rid, {"buy_item": {"vnum": 27001}, "expect_error": "NOT_ENOUGH_GOLD"})
    assert st["status"] == "passed"
    assert service.explorer.check(rid, [{"gold": {"equals": 10}}, "alive"])["passed"]
    out = service.explorer.finish(rid, findings=[{"title": "not", "severity": "note"}],
                                  save_as_scenario="explored_shop")
    assert out["result"] == "PASSED"
    rep = service.run_scenario("explored_shop", seed=ex["seed"])
    assert rep["result"] == "PASSED", rep["summary"]


def test_file_and_log_sources(tmp_path):
    ev = tmp_path / "qa_events.jsonl"
    log = tmp_path / "syserr"
    ev.write_text('{"type":"event","name":"OLD"}\n')
    log.write_text("eski satır\n")
    sig = ServerSignals([FileEventSource(ev), LogFileSource("syserr", log, ["SYSERR"])], characters={"AI_QA_001"})
    sig.mark()
    with ev.open("a") as f:
        f.write('{"type":"assert_fail","name":"PLAYER_NEGATIVE_GOLD","player":"AI_QA_001"}\n')
        f.write('{"type":"event","name":"OTHER","player":"AI_QA_999"}\n')
        f.write('{"type":"event","name":"PARTIAL"')  # yarım satır
    with log.open("a") as f:
        f.write("SYSERR: CHARACTER::UseItem: null item\nnormal satır\n")
    sig.poll()
    assert [e["name"] for e in sig.events] == ["PLAYER_NEGATIVE_GOLD", "SYSERR"]
    assert len(sig.assert_failures) == 1 and len(sig.errors) == 1
    with ev.open("a") as f:
        f.write("}\n")
    sig.poll()
    assert sig.named("PARTIAL")
