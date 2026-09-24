import pytest

from qa.replay import replay_run
from qa.scenario.runner import ScenarioRunner
from qa.session import BridgeFactory
from qa.sim.world import SimWorld
from qa.store.db import Store
from qa.scenario.loader import list_scenarios, load_scenario, parse_scenario

SCENARIOS = [s["name"] for s in list_scenarios(__import__("pathlib").Path(__file__).resolve().parents[1] / "scenarios")]


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("seed", [1, 846219])
def test_scenario_passes_on_healthy_sim(runner, cfg, name, seed):
    sc, text = load_scenario(cfg.scenarios_path, name)
    rep = runner.run(sc, text, seed=seed)
    assert rep["result"] == "PASSED", rep["summary"]
    run_dir = cfg.artifacts_path / rep["run_id"]
    for f in ("report.json", "trace.jsonl", "trace.txt", "client.log", "server.log", "scenario.yaml"):
        assert (run_dir / f).exists(), f


# fault -> (senaryo, beklenen ilk hata adı)
FAULT_MATRIX = [
    ("quest_off_by_one", "quest_dog_hunt", "quest"),
    ("no_drop", "kill_mob_pickup", "DROP_NOT_OBTAINED"),
    ("sell_no_gold", "npc_shop_buy_sell", "gold"),
    ("potion_no_heal", "use_equip_item", "hp"),
    ("syserr_on_equip", "use_equip_item", "server_errors"),
    ("negative_gold_on_buy", "shop_insufficient_gold", "expect_error"),
]


@pytest.mark.parametrize("fault,name,expected", FAULT_MATRIX)
def test_oracle_catches_injected_fault(runner, cfg, fault, name, expected):
    cfg.sim.faults = [fault]
    sc, text = load_scenario(cfg.scenarios_path, name)
    rep = runner.run(sc, text, seed=11)
    assert rep["result"] == "FAILED"
    assert rep["failure"]["name"] == expected, rep["summary"]
    assert rep["evidence"]["screenshots"] or rep["failure"]["kind"] == "oracle"
    assert runner.store.failures(rep["run_id"])


def test_negative_gold_also_trips_server_qa_assert(runner, cfg):
    cfg.sim.faults = ["negative_gold_on_buy"]
    sc, text = load_scenario(cfg.scenarios_path, "shop_insufficient_gold")
    rep = runner.run(sc, text, seed=3)
    names = [f["name"] for f in rep["failures"]]
    assert "qa_asserts" in names
    assert rep["server"]["qa_assert_failures"] == 1


def test_same_seed_same_trace(runner, cfg):
    sc, text = load_scenario(cfg.scenarios_path, "kill_mob_pickup")
    a = runner.run(sc, text, seed=99)
    b = runner.run(sc, text, seed=99)
    c = runner.run(sc, text, seed=100)
    assert a["trace_digest"] == b["trace_digest"]
    assert a["trace_digest"] != c["trace_digest"]


def test_replay_reproduces_and_detects_fix(runner, cfg):
    cfg.sim.faults = ["quest_off_by_one"]
    sc, text = load_scenario(cfg.scenarios_path, "quest_dog_hunt")
    rep = runner.run(sc, text)
    res = replay_run(runner, rep["run_id"], times=3)
    assert res["verdict"] == "REPRODUCED 3/3"
    assert res["deterministic_trace"]
    cfg.sim.faults = []  # "kod düzeltildi"
    res = replay_run(runner, rep["run_id"], times=2)
    assert res["verdict"] == "NOT REPRODUCED 0/2"


def test_step_failure_skips_rest(runner):
    sc = parse_scenario("""
name: t_skip
steps:
  - talk_npc: {vnum: 424242}
  - wait: 100
""")
    rep = runner.run(sc, seed=1)
    assert rep["result"] == "FAILED"
    assert rep["failure"]["name"] == "NOT_FOUND"
    assert [s["status"] for s in rep["steps"]] == ["failed", "skipped"]


def test_setup_error_is_infra_error(runner):
    sc = parse_scenario("""
name: t_setup
setup:
  - give_item: {vnum: 1, count: 1}
steps:
  - wait: 100
""")
    rep = runner.run(sc, seed=1)
    assert rep["result"] == "ERROR"
    assert "Setup" in rep["error"]


def test_wrong_password_is_login_failure(cfg):
    world = SimWorld(password="qa")
    cfg.accounts.password = "wrong"
    r = ScenarioRunner(cfg, Store(cfg.db_file), BridgeFactory(cfg, world))
    sc = parse_scenario("name: t_login\nsteps:\n  - wait: 10\n")
    rep = r.run(sc, seed=1)
    assert rep["result"] == "FAILED"
    assert rep["failure"]["step"] == 0 and rep["failure"]["name"] == "LOGIN_FAILED"


def test_readme_example_scenario(runner):
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    text = re.search(r"```yaml\n(name: npc_shop_buy_sell.*?)```", readme, re.S).group(1)
    rep = runner.run(parse_scenario(text), text, seed=5)
    assert rep["result"] == "PASSED", rep["summary"]


def test_full_scenario_over_tcp(cfg, tmp_path):
    """Gerçek client ile aynı yol: TCP bridge + ayrı süreçteki gibi çalışan sim sunucusu."""
    from qa.sim.server import serve_in_thread

    srv = serve_in_thread(port=0, world=SimWorld(password="qa"))
    try:
        cfg.bridge.mode, cfg.bridge.port = "tcp", srv.server_address[1]
        r = ScenarioRunner(cfg, Store(cfg.db_file))
        sc, text = load_scenario(cfg.scenarios_path, "quest_dog_hunt")
        rep = r.run(sc, text, seed=8)
        assert rep["result"] == "PASSED", rep["summary"]
        assert rep["client"]["client"] == "metin2_qa_sim"
    finally:
        srv.shutdown()
        srv.server_close()


def test_unreachable_client_is_infra_error(cfg):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # boş port: bağlantı reddedilir
    cfg.bridge.mode, cfg.bridge.port, cfg.bridge.timeout_s = "tcp", port, 2
    r = ScenarioRunner(cfg, Store(cfg.db_file))
    rep = r.run(parse_scenario("name: t_down\nsteps:\n  - wait: 10\n"), seed=1)
    assert rep["result"] == "ERROR" and "bağlanılamadı" in rep["error"]
    assert r.store.get_run(rep["run_id"])["status"] == "ERROR"
