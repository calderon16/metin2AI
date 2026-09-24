import pytest

from qa.replay import replay_run
from qa.scenario.loader import ScenarioError, dump_scenario, load_scenario, parse_scenario
from qa.scenario.runner import ScenarioRunner
from qa.sim.server import serve_in_thread
from qa.sim.world import SimWorld
from qa.store.db import Store

MULTI_FAULTS = [
    ("trade_item_dupe", "trade_item_for_gold", {("A", "inventory_not_contains"), (None, "conservation")}),
    ("trade_gold_dupe", "trade_item_for_gold", {("B", "gold"), (None, "conservation")}),
    ("trade_accept_not_reset", "trade_change_after_accept", {("A", "trade")}),
    ("party_exp_dupe", "party_exp_share", {("A", "player.exp"), ("B", "player.exp")}),
]


@pytest.mark.parametrize("fault,name,expected", MULTI_FAULTS)
def test_multi_agent_fault_caught(runner, cfg, fault, name, expected):
    cfg.sim.faults = [fault]
    rep = runner.run(*load_scenario(cfg.scenarios_path, name), seed=5)
    assert rep["result"] == "FAILED"
    assert {(f.get("agent"), f["name"]) for f in rep["failures"]} == expected


def test_report_and_trace_carry_agents(runner, cfg):
    rep = runner.run(*load_scenario(cfg.scenarios_path, "trade_item_for_gold"), seed=2)
    assert rep["result"] == "PASSED"
    assert rep["agents"] == {"A": "AI_QA_001", "B": "AI_QA_002"}
    trace = (cfg.artifacts_path / rep["run_id"] / "trace.txt").read_text(encoding="utf-8")
    assert "A: trade_request" in trace and "B:   <- trade_started" in trace
    assert "== A (AI_QA_001) ==" in (cfg.artifacts_path / rep["run_id"] / "client.log").read_text(encoding="utf-8")
    assert all("agent" in s for s in rep["steps"])


def test_multi_agent_replay_is_deterministic(runner, cfg):
    cfg.sim.faults = ["party_exp_dupe"]
    rep = runner.run(*load_scenario(cfg.scenarios_path, "party_exp_share"))
    res = replay_run(runner, rep["run_id"], times=2)
    assert res["verdict"] == "REPRODUCED 2/2" and res["deterministic_trace"]


def test_multi_agent_over_tcp(cfg):
    srv = serve_in_thread(port=0, world=SimWorld(password="qa"))
    try:
        port = srv.server_address[1]
        cfg.bridge.mode = "tcp"
        # Sim sunucusu tek porttan birden çok istemci kabul eder; gerçek client'ta her ajan ayrı port
        cfg.bridge.agent_ports = {"AI_QA_001": port, "AI_QA_002": port}
        rep = ScenarioRunner(cfg, Store(cfg.db_file)).run(*load_scenario(cfg.scenarios_path, "trade_item_for_gold"),
                                                          seed=4)
        assert rep["result"] == "PASSED", rep["summary"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_agent_ports_fallback(cfg):
    from qa.session import BridgeFactory

    cfg.bridge.mode, cfg.bridge.port = "tcp", 50000
    f = BridgeFactory(cfg)
    assert f.open("AI_QA_001", 0).port == 50000 and f.open("AI_QA_002", 1).port == 50001
    cfg.bridge.agent_ports = {"AI_QA_002": 51000}
    assert f.open("AI_QA_002", 1).port == 51000


@pytest.mark.parametrize("bad,msg", [
    ("name: x\nagents: {A: {account: AI_QA_001}}\nsteps:\n  - wait: 1\n    agent: C\n", "bilinmeyen ajan 'C'"),
    ("name: x\nsteps:\n  - wait: 1\n    agent: A\n", "bilinmeyen ajan"),
    ("name: x\nagents: {A: {account: AI_QA_001}, B: {account: AI_QA_001}}\n", "farklı olmalı"),
    ("name: x\naccount: AI_QA_001\nagents: {A: {account: AI_QA_002}}\n", "agents kullanılırken"),
    ("name: x\nagents: {A: {account: AI_QA_001}}\nassert:\n  - alive:\n    agent: Z\n", "bilinmeyen ajan 'Z'"),
    ("name: x\nagents: {A: {account: AI_QA_001}}\nassert:\n  - alive:\n    label: q\n", "kullanılamaz"),
])
def test_agent_validation(bad, msg):
    with pytest.raises(ScenarioError, match=msg):
        parse_scenario(bad)


def test_multi_agent_roundtrip(cfg):
    sc, _ = load_scenario(cfg.scenarios_path, "trade_change_after_accept")
    again = parse_scenario(dump_scenario(sc))
    assert again.model_dump() == sc.model_dump()


def test_non_qa_agent_account_rejected(runner):
    sc = parse_scenario("name: x\nagents: {A: {account: AI_QA_001}, B: {account: RealPlayer}}\nsteps:\n  - wait: 1\n")
    with pytest.raises(Exception, match="QA hesabı değil"):
        runner.run(sc, seed=1)


def test_unknown_peer_in_behaviour(runner):
    sc = parse_scenario("name: x\nagents: {A: {account: AI_QA_001}}\nsteps:\n  - trade_with: {agent: B}\n")
    rep = runner.run(sc, seed=1)
    assert rep["failure"]["name"] == "UNKNOWN_AGENT"
