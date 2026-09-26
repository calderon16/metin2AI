import os
import time
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
    with pytest.raises(ValueError, match="Desteklenmeyen"):
        make_crypto("rc4")


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
        b.call("party_invite", vid=1)
    b.close()


def test_headless_walks_like_a_player(server, prof):
    b = _login(server, prof)
    b.call("move_to", x=7000, y=5000)
    b.call("wait", ms=2200)
    assert b.call("get_player_state")["x"] == 7000
    assert server.world.stats["moves"] >= 10 and server.world.stats["speed_violations"] == 0
    assert server.world.stats["max_time_skew_ms"] < 30000       # hareket saati sunucu saatine yakın
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
  - system_message: cevap 0      # gerçek istemci gibi 0 tabanlı seçenek sırası
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


def test_bulk_jobs_only_pick_scenarios_for_the_bridge_mode(cfg, monkeypatch):
    from qa.service import QaService

    fake = [{"name": "sim_only", "tags": ["smoke"]}, {"name": "real_walk", "tags": ["real"]},
            {"name": "real_gui", "tags": ["real", "real_only", "needs_client"]},
            {"name": "real_quest", "tags": ["real", "real_only", "quest"]}]
    monkeypatch.setattr(QaService, "list_scenarios", lambda self, tag=None: [s for s in fake
                                                                           if tag is None or tag in s["tags"]])
    names = lambda: {s["name"] for s in QaService(cfg).runnable_scenarios()}  # noqa: E731
    cfg.bridge.mode = "sim"
    assert names() == {"sim_only", "real_walk"}
    cfg.bridge.mode = "headless"
    assert names() == {"real_walk", "real_quest"}           # needs_client (görsel) hariç
    cfg.bridge.mode = "tcp"
    assert names() == {"real_walk", "real_gui", "real_quest"}


def test_daemon_learning_jobs(server, prof, cfg, tmp_path, monkeypatch):
    """Veri toplama rotasyonu sırayla ilerler; öğrenme döngüsü az veride eğitime geçmez."""
    from qa.daemon.core import Daemon
    from qa.planner.llm import ScriptedProvider

    ppath = tmp_path / "fx.json"
    prof.save(ppath)
    cfg.bridge.mode = "headless"
    cfg.headless = _hcfg(server, profile=str(ppath))
    cfg.daemon.agents = [{"account": "AI_QA_001"}]
    cfg.daemon.snapshot_interval_s = 0.2
    goals = tmp_path / "goals.yaml"
    goals.write_text("goals:\n  - {id: g1, goal: yürü, steps: 3}\n  - {id: g2, goal: bak, steps: 3}\n", encoding="utf-8")
    script = [{"name": "walk_by", "args": {"dx": 100, "dy": 0}}, {"name": "finish", "args": {"summary": "tamam"}}]
    d = Daemon(cfg, llm_factory=lambda: ScriptedProvider(list(script)))
    d.start()
    try:
        assert d.agents.wait_online(1, 15)
        ran = []
        for _ in range(3):
            j = d.jobs.wait(d.jobs.submit("explore_rotation", {"goals": str(goals)}), 60)
            assert j["status"] == "done", j
            ran.append(j["result"]["goal"])
        assert ran == ["g1", "g2", "g1"]
        import training.cycle as cyc
        monkeypatch.setattr(cyc, "ROOT", tmp_path)                 # veri tmp'ye yazılsın
        j = d.jobs.wait(d.jobs.submit("learning_cycle", {"min_samples": 10_000}), 60)
        assert j["status"] == "done" and j["result"]["status"] == "collecting", j
        assert j["result"]["dataset"]["samples"] >= 1
    finally:
        d.shutdown()


def test_headless_refuses_to_walk_off_the_map(server, prof):
    b = _login(server, prof)
    with pytest.raises(ActionError, match="OUT_OF_MAP"):
        b.call("move_to", x=5000, y=-100)        # harita 0..25600; y < 0 dışarıda
    b.call("move_to", x=5200, y=5000)            # içeride: kabul
    b.close()


def test_quest_id_resolution_helps_small_models():
    from qa.engine.behaviours import BehaviourError, _resolve_quest

    st = {"s1_1": {"title": "Yeni Nöbetçi", "state": "available", "target": {"npc": 20354}},
          "s1_2": {"title": "Erken Gelen Kış", "state": "available"}}
    assert _resolve_quest(st, "s1_1") == "s1_1"
    assert _resolve_quest(st, "yeni nöbetçi") == "s1_1"                            # başlıkla
    assert _resolve_quest(st, "Köyde sana verilecek yeni bir görev var.") == "s1_1"  # işaretli tek görev
    with pytest.raises(BehaviourError, match=r"Geçerli kimlikler: a \(A, active\), b"):
        _resolve_quest({"a": {"title": "A", "state": "active"}, "b": {"title": "B", "state": "active"}}, "x")


def test_whisper_roundtrip(server, prof):
    b = _login(server, prof)
    b.call("whisper", to="TESTR", message="Merhaba sahip")
    b.call("wait", ms=200)
    assert server.world.whispers_to_owner == ["Merhaba sahip"]
    with pytest.raises(ActionError, match="WHISPER_FAILED"):
        b.call("whisper", to="Yok_Biri", message="x")
    assert server.owner_whisper("köye dön") == 1
    b.call("wait", ms=200)
    w = b.call("get_whispers")
    assert w[-1]["from"] == "TESTR" and w[-1]["text"] == "köye dön" and w[-1]["gm"] is True
    assert b.call("get_whispers", since=w[-1]["seq"]) == []
    b.close()


def test_daemon_turns_owner_whisper_into_a_job(server, prof, cfg, tmp_path):
    from qa.daemon.core import Daemon
    from qa.planner.llm import ScriptedProvider

    ppath = tmp_path / "fx.json"
    prof.save(ppath)
    cfg.bridge.mode = "headless"
    cfg.headless = _hcfg(server, profile=str(ppath))
    cfg.daemon.agents = [{"account": "AI_QA_001"}, {"account": "AI_QA_002"}]
    cfg.daemon.owners = ["TESTR"]
    cfg.daemon.snapshot_interval_s = 0.2
    from qa.planner.llm import ToolCall

    def make():
        n = {"i": 0}

        def script(messages):
            first = messages[0].text or ""
            if "fısıltıyla" not in first and "HEDEF" not in first:
                # sohbet adımı: mesaja doğal cevap + iş
                return '{"reply": "Tamam, hallediyorum.", "task": "biraz yürü"}'
            i = n["i"]
            n["i"] += 1
            steps = [ToolCall("walk_by", {"dx": 100, "dy": 0}, "w"),
                     ToolCall("whisper", {"to": "TESTR", "message": "Yürüdüm."}, "m"),
                     ToolCall("finish", {"summary": "istek yapıldı"}, "f")]
            return [steps[min(i, 2)]]
        return ScriptedProvider(script)

    d = Daemon(cfg, llm_factory=make)
    d.start()
    try:
        assert d.agents.wait_online(2, 15)
        assert server.owner_whisper("biraz yürü") == 2        # iki ajana da ulaşır
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and len(server.world.whispers_to_owner) < 4:
            time.sleep(0.2)
        jobs = [j for j in d.db.list_jobs(None, 20) if j["type"] == "owner_command"]
        assert {j["params"]["account"] for j in jobs} == {"AI_QA_001", "AI_QA_002"}
        assert all(j["source"] == "whisper:TESTR" for j in jobs)
        # önce işin kendisi söylenir, rapor adımların gerçek sonucundan gelir (model sahibine yazamaz)
        assert server.world.whispers_to_owner.count("Tamam, şunu yapıyorum: biraz yürü") == 2
        assert server.world.whispers_to_owner.count("Yaptıklarım: yürüdüm.") == 2
    finally:
        d.shutdown()


def test_player_mode_plays_without_teleport_and_yields_to_owner(server, prof, cfg, tmp_path):
    """Oyuncu modu: /qa reset ve qa_setup yok; sahip fısıldayınca oturum kesilir, komut hemen yürür."""
    from qa.daemon.core import Daemon
    from qa.planner.llm import ScriptedProvider, ToolCall

    ppath = tmp_path / "fx.json"
    prof.save(ppath)
    cfg.bridge.mode = "headless"
    cfg.headless = _hcfg(server, profile=str(ppath))
    cfg.daemon.agents = [{"account": "AI_QA_001"}]
    cfg.daemon.owners = ["TESTR"]
    cfg.daemon.player_mode = True
    cfg.daemon.player_session_steps = 200
    cfg.daemon.snapshot_interval_s = 0.2
    seen: list[tuple[str, set[str]]] = []

    class Rec(ScriptedProvider):
        def chat(self, system, messages, tools):
            seen.append((system, {t.name for t in tools}))
            return super().chat(system, messages, tools)

    def make():
        n = {"i": 0}

        def script(messages):
            i = n["i"]
            n["i"] += 1
            first = messages[0].text or ""
            if "fısıltıyla" not in first and "HEDEF" not in first:
                return "Tamam"      # sohbet cevabı (selam gibi kurallarla anlaşılan mesajlarda)
            return [ToolCall("wait", {"ms": 300}, f"p{i}")]
        return Rec(script)

    d = Daemon(cfg, llm_factory=make)
    d.start()
    try:
        assert d.agents.wait_online(1, 15)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not any(j["type"] == "play_session" and j["status"] == "running"
                                                      for j in d.db.list_jobs(None, 20)):
            time.sleep(0.2)
        time.sleep(1.5)
        assert server.owner_whisper("selam") == 1
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "Tamam" not in server.world.whispers_to_owner:
            time.sleep(0.2)
        assert "Tamam" in server.world.whispers_to_owner
        jobs = d.db.list_jobs(None, 20)
        play = next(j for j in jobs if j["type"] == "play_session" and j["status"] != "running")
        assert play["result"]["stop_reason"] == "preempted"
        owner = next(j for j in jobs if j["type"] == "owner_command")
        deadline = time.monotonic() + 10
        while owner["status"] != "done" and time.monotonic() < deadline:
            time.sleep(0.2)
            owner = d.db.get_job(owner["job_id"])
        # sohbet mesajı: cevap verildi, iş (keşif) başlatılmadı
        assert owner["result"]["reply"] == "Tamam" and owner["result"]["intent"] == "chat"
        assert not owner["result"].get("task_run_id")
        # "gel": ajan yola çıkar; sahibi göremiyorsa bunu doğru söyler (yalan "geldim" yok)
        n = len(server.world.whispers_to_owner)
        assert server.owner_whisper("buraya gel") == 1
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(server.world.whispers_to_owner) < n + 2:
            time.sleep(0.2)
        assert server.world.whispers_to_owner[n:n + 2] == ["Geliyorum.", "Seni göremiyorum; hangi haritada ve neredesin?"], [(j["type"], j["status"], (j["error"] or "")[-600:], j["progress"]) for j in d.db.list_jobs(None, 5) if j["type"] == "owner_command"]
        assert not [c for c in server.world.stats.get("qa_commands", []) if "reset" in c or "warp" in c]
        assert seen and all("qa_setup" not in tools for _, tools in seen)
        assert any("normal bir oyuncusun" in s for s, _ in seen)
        from qa.daemon.server import Api

        info = Api(d).dispatch("GET", "/api/player", {}, None)
        assert info["enabled"] and "AI_QA_001" in info["agents"] and info["owners"] == ["TESTR"]
        off = Api(d).dispatch("POST", "/api/player", {}, {"enabled": False})
        assert off["enabled"] is False
        assert not [j for j in d.db.list_jobs("queued", 50) if j["type"] == "play_session"]
    finally:
        d.shutdown()
