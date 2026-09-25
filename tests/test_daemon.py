import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

from qa.daemon.core import Daemon
from qa.daemon.scheduler import is_due, parse_daily, parse_every, validate
from qa.daemon.server import PanelServer
from qa.planner.llm import ScriptedProvider


@pytest.fixture
def daemon(cfg):
    cfg.daemon.snapshot_interval_s = 0.1
    cfg.daemon.reconnect_backoff_s = [0.1]
    d = Daemon(cfg)
    d.lease_timeout_s = 20
    d.start()
    assert d.agents.wait_online(4, 10)
    yield d
    d.shutdown()


def test_agents_stay_online_between_jobs(daemon):
    j = daemon.jobs.wait(daemon.jobs.submit("scenario", {"name": "npc_shop_buy_sell", "seed": 1}), 60)
    assert j["status"] == "done" and j["result"]["result"] == "PASSED"
    time.sleep(0.3)
    assert {a["status"] for a in daemon.agents.list()} == {"online"}
    # işler arasında logout olmadı: ajan hâlâ oyunda
    assert all(a["snapshot"].get("in_game") for a in daemon.agents.list())


def test_suite_and_multi_agent_lease(daemon):
    j = daemon.jobs.wait(daemon.jobs.submit("suite", {"tag": "trade", "seed": 2}), 90)
    assert j["status"] == "done", j.get("error")
    assert j["result"]["counts"] == {"PASSED": 3}
    assert len(j["agents"]) == 2 and len(j["run_ids"]) == 3


def test_reconnect_after_drop(daemon):
    a = daemon.agents.agents["AI_QA_003"]
    # Bağlantı kopmasını taklit et: karakteri sunucu tarafında oyundan düşür
    with daemon.agents.world_guard():
        daemon.agents.world.players["AI_QA_003"].in_game = False
    deadline = time.time() + 10
    while time.time() < deadline and a.reconnects == 0:
        time.sleep(0.05)
    assert a.reconnects >= 1
    assert daemon.agents.wait_online(4, 10)


def test_stop_start_and_not_enough_agents(daemon):
    for acc in ("AI_QA_002", "AI_QA_003", "AI_QA_004"):
        daemon.agents.disable(acc)
    assert daemon.agents.agents["AI_QA_002"].status == "stopped"
    j = daemon.jobs.wait(daemon.jobs.submit("scenario", {"name": "trade_item_for_gold"}), 30)
    assert j["status"] == "failed" and "2 ajan" in j["error"]
    daemon.agents.enable("AI_QA_002")
    assert daemon.agents.wait_online(2, 10)
    j = daemon.jobs.wait(daemon.jobs.submit("scenario", {"name": "trade_item_for_gold", "seed": 3}), 30)
    assert j["status"] == "done" and j["result"]["result"] == "PASSED"


def test_account_guard(daemon):
    with pytest.raises(Exception, match="QA hesabı değil"):
        daemon.agents.add("RealPlayer")


def test_cancel_queued_and_validation(daemon):
    with pytest.raises(ValueError, match="Bilinmeyen iş tipi"):
        daemon.jobs.submit("rm_rf", {})
    with pytest.raises(Exception):
        daemon.jobs.submit("scenario", {"name": "yok_boyle_senaryo"})
    with pytest.raises(ValueError, match="LLM"):
        daemon.jobs.submit("explore", {"goal": "x"})
    ids = [daemon.jobs.submit("suite", {"seed": 1}) for _ in range(3)]
    daemon.jobs.cancel(ids[-1])
    assert daemon.db.get_job(ids[-1])["status"] == "cancelled"
    daemon.jobs.cancel(ids[0])  # çalışıyor olabilir: iptal bayrağı
    for jid in ids[:2]:
        assert daemon.jobs.wait(jid, 120)["status"] in ("done", "cancelled")


def test_findings_confirm_fix_and_regress(daemon):
    daemon.agents.world.faults = {"sell_no_gold"}
    j = daemon.jobs.wait(daemon.jobs.submit("scenario", {"name": "npc_shop_buy_sell", "seed": 4}), 60)
    assert j["result"]["result"] == "FAILED"
    [f] = daemon.db.list_findings()
    assert f["status"] == "confirmed" and f["confirm"]["verdict"] == "REPRODUCED 3/3"
    assert f["system"] == "npc_shop"
    # aynı hata tekrar → yeni kayıt açılmaz
    daemon.jobs.wait(daemon.jobs.submit("scenario", {"name": "npc_shop_buy_sell"}), 60)
    [f] = daemon.db.list_findings()
    assert f["occurrences"] == 2
    # "kod düzeltildi" → geçen run bulguyu kapatır
    daemon.agents.world.faults = set()
    daemon.jobs.wait(daemon.jobs.submit("scenario", {"name": "npc_shop_buy_sell"}), 60)
    assert daemon.db.get_finding(f["id"])["status"] == "fixed"
    # tekrar bozulursa → geriledi (sonra doğrulanır)
    daemon.agents.world.faults = {"sell_no_gold"}
    daemon.jobs.wait(daemon.jobs.submit("scenario", {"name": "npc_shop_buy_sell"}), 60)
    assert daemon.db.get_finding(f["id"])["status"] in ("regressed", "confirmed")
    assert daemon.db.get_finding(f["id"])["occurrences"] == 3


def test_campaign_matrix_and_regressions(daemon):
    j1 = daemon.jobs.wait(daemon.jobs.submit("campaign", {"seed": 1}), 180)
    c1 = daemon.db.get_campaign(j1["result"]["campaign_id"])
    assert c1["matrix"]["trade"]["status"] == "passed" and c1["matrix"]["guild"]["status"] == "untested"
    daemon.agents.world.faults = {"trade_item_dupe"}
    j2 = daemon.jobs.wait(daemon.jobs.submit("campaign", {"seed": 1, "systems": ["trade", "npc_shop"]}), 180)
    s = j2["result"]
    assert s["counts"] == {"failed": 1, "passed": 1} and s["regressions"] == ["trade"]
    assert s["previous_campaign"] == c1["id"]
    j3 = daemon.jobs.wait(daemon.jobs.submit("campaign", {"systems": ["yok"]}), 10)
    assert j3["status"] == "failed" and "Katalogda olmayan" in j3["error"]


def test_explore_job_with_scripted_llm(cfg):
    script = [{"name": "talk_npc", "args": {"vnum": 9001}},
              {"name": "report_finding", "args": {"title": "Test bulgusu", "description": "x", "severity": "major"}},
              {"name": "finish", "args": {"summary": "ok"}}]
    d = Daemon(cfg, llm_factory=lambda: ScriptedProvider(list(script)))
    d.start()
    try:
        assert d.agents.wait_online(4, 10)
        j = d.jobs.wait(d.jobs.submit("explore", {"goal": "dükkan", "max_steps": 5}), 60)
        assert j["status"] == "done" and j["result"]["stop_reason"] == "finished"
        [f] = d.db.list_findings()
        assert f["title"] == "Test bulgusu" and f["severity"] == "major" and f["kind"] == "finding"
    finally:
        d.shutdown()


# ---------------------------------------------------------------------- zamanlayıcı

def test_schedule_parsing():
    assert parse_every("30m") == 1800 and parse_every("2h") == 7200
    assert parse_daily("03:05") == (3, 5)
    with pytest.raises(ValueError):
        parse_every("often")
    with pytest.raises(ValueError):
        validate([{"name": "a", "every": "1h", "daily": "03:00", "job": {"type": "suite"}}], {"suite": ""})
    with pytest.raises(ValueError):
        validate([{"name": "a", "every": "1h", "job": {"type": "nope"}}], {"suite": ""})


def test_is_due():
    now = datetime(2026, 9, 25, 3, 10).astimezone()
    daily = {"name": "d", "daily": "03:00", "job": {}}
    assert is_due(daily, None, now, False)
    assert not is_due(daily, now - timedelta(minutes=5), now, False)
    assert is_due(daily, now - timedelta(hours=20), now, False)
    assert not is_due(daily, None, now.replace(hour=12), False)
    every = {"name": "e", "every": "1h", "job": {}}
    assert is_due(every, now - timedelta(hours=2), now, False)
    assert not is_due(every, now - timedelta(minutes=10), now, False)
    assert not is_due({"name": "c", "continuous": True}, None, now, True)
    assert is_due({"name": "c", "continuous": True}, None, now, False)


def test_scheduler_creates_jobs(cfg):
    cfg.daemon.schedules = [{"name": "smoke", "every": "1h", "job": {"type": "suite", "tag": "smoke"}}]
    d = Daemon(cfg)
    d.start()
    try:
        [jid] = d.scheduler.tick()
        assert d.db.get_job(jid)["source"] == "schedule:smoke"
        assert d.scheduler.tick() == []  # iş sürerken / 1 saat dolmadan yenisi yok
        assert d.jobs.wait(jid, 60)["status"] == "done"
    finally:
        d.shutdown()


# ---------------------------------------------------------------------- HTTP API

def _req(url, method="GET", body=None, token=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


@pytest.fixture
def panel(daemon, monkeypatch):
    monkeypatch.setenv("QA_PANEL_TOKEN", "gizli")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    srv = PanelServer(daemon, "127.0.0.1", 0)
    srv.start()
    yield srv
    srv.shutdown()


def test_api_auth_and_static(panel):
    st, ct, body = _req(panel.url + "/")
    assert st == 200 and "text/html" in ct and b"Metin2 AI QA" in body
    assert _req(panel.url + "/app.js")[0] == 200
    assert _req(panel.url + "/api/status")[0] == 401
    assert _req(panel.url + "/api/status", token="yanlis")[0] == 401
    st, _, body = _req(panel.url + "/api/status", token="gizli")
    assert st == 200 and json.loads(body)["agents"]["online"] == 4


def test_api_flow(panel):
    t = "gizli"
    st, _, body = _req(panel.url + "/api/jobs", "POST", {"type": "scenario", "params": {"name": "use_equip_item"}}, t)
    assert st == 200
    jid = json.loads(body)["job_id"]
    job = panel.daemon.jobs.wait(jid, 60)
    rid = job["run_ids"][0]
    rep = json.loads(_req(f"{panel.url}/api/runs/{rid}", token=t)[2])
    assert rep["result"] == "PASSED"
    assert "equip_item" in json.loads(_req(f"{panel.url}/api/runs/{rid}/trace", token=t)[2])["text"]
    # Kaçış denemesi
    assert _req(f"{panel.url}/api/runs/{rid}/files/..%2F..%2Fqa.sqlite", token=t)[0] == 404
    assert _req(panel.url + "/api/jobs", "POST", {"type": "nope"}, t)[0] == 400
    assert _req(panel.url + "/api/jobs/99999", token=t)[0] == 404
    assert _req(panel.url + "/api/agents/AI_QA_004/stop", "POST", {}, t)[0] == 200
    assert panel.daemon.agents.agents["AI_QA_004"].status == "stopped"
    st, _, body = _req(panel.url + "/api/agents", "POST", {"account": "Hacker"}, t)
    assert st == 400
    cat = json.loads(_req(panel.url + "/api/catalog", token=t)[2])
    assert any(s["id"] == "trade" and "trade_item_for_gold" in s["scenarios"] for s in cat)
    assert _req(panel.url + "/api/scenarios/smoke_login_walk", token=t)[0] == 200


def test_panel_requires_token_off_loopback(daemon, monkeypatch):
    monkeypatch.delenv("QA_PANEL_TOKEN", raising=False)
    with pytest.raises(Exception, match="QA_PANEL_TOKEN"):
        PanelServer(daemon, "0.0.0.0", 0)
