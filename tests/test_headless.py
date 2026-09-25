import os
from pathlib import Path

import pytest

from qa.bridge.client import LocalBridge
from qa.bridge.protocol import ActionError
from qa.config import HeadlessConfig
from qa.headless.bindings import Bindings
from qa.headless.client import HeadlessClient
from qa.headless.crypto import XteaCrypto, make_crypto
from qa.headless.fakeserver import FakeMetin2
from qa.headless.profile import Profile, build_profile, import_profile, preprocess
from qa.scenario.loader import parse_scenario
from qa.scenario.runner import ScenarioRunner
from qa.store.db import Store

F = Path(__file__).parent / "fixtures" / "headless"


@pytest.fixture(scope="module")
def prof():
    return import_profile([F / "packet_sample.h"], [F / "PythonNetworkStream.cpp", F / "packet_info.cpp"], [], [], "fx")


@pytest.fixture
def server(prof):
    s = FakeMetin2(prof)
    yield s
    s.shutdown()


def _hcfg(server, **kw):
    return HeadlessConfig(auth_port=server.auth_port, channels={"1": server.game_port}, timeout_s=5,
                          move_interval_ms=100, walk_speed=1500,
                          maps=[{"index": 1, "x": 0, "y": 0, "width": 25600, "height": 25600}], **kw)


# ---------------------------------------------------------------------- profil

def test_profile_parsing(prof):
    assert prof.header("HEADER_CG_CHAT") == 3                  # örtük enum artışı
    assert prof.header("HEADER_GC_HANDSHAKE") == 0xFF          # hex
    assert prof.sizeof("TSimplePlayer") == 63
    assert prof.sizeof("TPacketGCLoginSuccess4") == 329       # iç içe struct dizisi + 2 boyutlu char
    assert prof.packets["HEADER_GC_CHAT"]["dynamic"]           # client tablosundan
    assert prof.packets["HEADER_CG_CHAT"]["dynamic"]           # size alanından çıkarım
    assert not prof.packets["HEADER_CG_MOVE"]["dynamic"]
    assert "TPacketShouldNotExist" not in prof.structs         # #ifdef dalı
    assert prof.constants["PHASE_AUTH"] == 10 and prof.constants["PHASE_GAME"] == 5


def test_preprocessor():
    d = {"A": "1"}
    out = preprocess("#if defined(A) && !defined(B)\nyes\n#elif 1\nno\n#else\nno2\n#endif\n#ifdef B\nx\n#endif\n#define C 5", d)
    assert out.split() == ["yes"] and d["C"] == "5"


def test_encode_decode_roundtrip(prof):
    v = {"bHeader": 32, "players": [{"dwID": 7, "szName": "AI_QA_Çğü", "byLevel": 99, "x": -5, "y": 12}],
         "guild_name": ["Lonca", "", "", ""], "handle": 3}
    raw = prof.encode("TPacketGCLoginSuccess4", v)
    assert len(raw) == 329
    d, off = prof.decode("TPacketGCLoginSuccess4", raw)
    assert off == 329 and d["players"][0]["szName"] == "AI_QA_Çğü" and d["players"][0]["x"] == -5
    assert d["guild_name"][0] == "Lonca" and d["players"][1]["szName"] == ""


def test_profile_json_roundtrip(prof, tmp_path):
    p = tmp_path / "p.json"
    prof.save(p)
    again = Profile.load(p)
    assert again.sizeof("TPacketGCLoginSuccess4") == 329 and again.packets == prof.packets


def test_struct_guess_without_size_table():
    p = build_profile(["enum { HEADER_GC_ITEM_SET = 5 };\ntypedef struct x { BYTE header; DWORD vnum; } TPacketGCItemSet;"],
                      [], {})
    assert p.packets["HEADER_GC_ITEM_SET"]["struct"] == "TPacketGCItemSet"


def test_xtea_roundtrip():
    c = XteaCrypto(bytes(range(16)))
    data = b"merhaba metin2 paket"
    enc = c.encrypt(data)
    assert len(enc) % 8 == 0 and enc != data.ljust(len(enc), b"\0")
    dec = XteaCrypto(bytes(range(16)))
    out = dec.decrypt(enc[:5]) + dec.decrypt(enc[5:])   # parçalı gelse de çözülür
    assert out.rstrip(b"\0") == data
    with pytest.raises(ValueError, match="kaynak kod"):
        make_crypto("improved")


def test_bindings_overrides(prof):
    b = Bindings(prof, {"points": {"GOLD": 12}, "packets": {"select": {"cg": ["HEADER_CG_YOK", "HEADER_CG_CHARACTER_SELECT"]}}})
    assert b["points"]["GOLD"] == 12 and b["points"]["HP"] == 5
    assert b.cg("select") == "HEADER_CG_CHARACTER_SELECT"
    assert not Bindings(prof, {"packets": {"select": {"cg": ["HEADER_CG_YOK"]}}}).supports("select")


# ---------------------------------------------------------------------- client

def _login(server, prof, account="AI_QA_001"):
    c = HeadlessClient(_hcfg(server), profile=prof)
    b = LocalBridge(c.handle, on_close=c.close)
    b.connect()
    b.call("login", account=account, password="qa")
    b.call("select_character", name=account)
    return b


def test_headless_full_flow(server, prof):
    b = _login(server, prof)
    st = b.call("get_player_state")
    assert st["in_game"] and st["name"] == "AI_QA_001" and st["hp"] == 400 and st["level"] == 10
    assert st["map"] == 1
    ents = {e["vid"]: e for e in b.call("get_nearby_entities")}
    assert ents[200]["type"] == "monster" and ents[200]["name"] == "Yaban Köpeği" and ents[300]["type"] == "npc"
    b.call("target", vid=200)
    b.call("attack")
    b.call("wait", ms=3000)
    evs = [e["event"] for e in b.drain_events()]
    assert "entity_dead" in evs and "item_dropped" in evs
    b.call("pickup", vid=400)
    assert {i["vnum"] for i in b.call("get_inventory")["items"]} == {27001, 30000}
    b.call("send_chat", message="/qa gold 500")
    b.call("wait", ms=200)
    assert b.call("get_player_state")["gold"] == 500
    assert b.call("get_system_messages")[-1]["text"] == "[QA] OK gold"
    assert server.world.stats["pongs"] == 1
    with pytest.raises(ActionError, match="NOT_SUPPORTED"):
        b.call("trade_request", vid=1)
    b.close()


def test_headless_walks_like_a_player(server, prof):
    b = _login(server, prof)
    b.call("move_to", x=7000, y=5000)
    b.call("wait", ms=2200)
    assert b.call("get_player_state")["x"] == 7000
    assert server.world.stats["moves"] >= 10 and server.world.stats["speed_violations"] == 0
    b.close()


def test_headless_login_failures(server, prof):
    c = HeadlessClient(_hcfg(server), profile=prof)
    with pytest.raises(ActionError, match="LOGIN_FAILED"):
        LocalBridge(c.handle).call("login", account="AI_QA_001", password="yanlis")
    c2 = HeadlessClient(HeadlessConfig(auth_port=1, timeout_s=1), profile=prof)
    with pytest.raises(ActionError, match="DISCONNECTED"):
        LocalBridge(c2.handle).call("login", account="AI_QA_001", password="qa")


def test_scenario_over_headless(server, prof, cfg, tmp_path):
    ppath = tmp_path / "fx.json"
    prof.save(ppath)
    cfg.bridge.mode = "headless"
    cfg.headless = _hcfg(server, profile=str(ppath))
    sc = parse_scenario("""
name: headless_smoke
setup:
  - set_gold: 250
steps:
  - kill_monster: {vnum: 101, count: 1}
  - pickup: {vnum: 30000}
  - use_item: {vnum: 27001}
  - talk_npc: {vnum: 9001}
    expect:
      - window_open: dialog
  - select_dialog: {text: Görev}
assert:
  - inventory_contains: 30000
  - item_count: {vnum: 27001, delta: -1}
  - gold: {equals: 250}
  - system_message: cevap 1
""")
    rep = ScenarioRunner(cfg, Store(cfg.db_file)).run(sc, seed=1)
    assert rep["result"] == "PASSED", rep["summary"]
    assert rep["client"]["client"] == "metin2_qa_headless"
    assert rep["steps"][0]["status"] == "passed"


def test_daemon_with_headless_agents(server, prof, cfg, tmp_path):
    from qa.daemon.core import Daemon

    ppath = tmp_path / "fx.json"
    prof.save(ppath)
    cfg.bridge.mode = "headless"
    cfg.headless = _hcfg(server, profile=str(ppath))
    cfg.daemon.agents = [{"account": "AI_QA_001"}, {"account": "AI_QA_002"}]
    cfg.daemon.snapshot_interval_s = 0.2
    d = Daemon(cfg)
    d.start()
    try:
        assert d.agents.wait_online(2, 15)
        assert d.jobs.workers == cfg.daemon.max_parallel_jobs   # gerçek sunucu modunda paralel
        j = d.jobs.wait(d.jobs.submit("scenario", {"name": "smoke_login_walk"}), 60)
        assert j["status"] == "done" and j["result"]["result"] == "PASSED", j["result"]
        assert server.world.stats["logins"] == 2                # ajanlar işler arasında yeniden login olmadı
    finally:
        d.shutdown()
