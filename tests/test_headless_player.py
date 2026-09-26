"""Headless normal oyuncu eylemleri (sahte sunucuya karşı): NPC dükkânı, demircide + basma, beceri, ekipman,
eşya bırakma. Paket biçimleri Metin2Re istemci/sunucu kaynağıyla aynı (tests/fixtures/headless/packet_player.h)."""

from pathlib import Path

import pytest

from qa.bridge.client import LocalBridge
from qa.bridge.protocol import ActionError
from qa.config import HeadlessConfig
from qa.headless.client import HeadlessClient
from qa.headless.fakeserver import SHOP_VID, SMITH_VID, FakeMetin2
from qa.headless.profile import import_profile

F = Path(__file__).parent / "fixtures" / "headless"


@pytest.fixture(scope="module")
def prof():
    return import_profile([F / "packet_sample.h", F / "packet_player.h"],
                          [F / "PythonNetworkStream.cpp", F / "packet_info.cpp"], [], [], "fx-player")


@pytest.fixture
def b(prof):
    s = FakeMetin2(prof)
    cfg = HeadlessConfig(auth_port=s.auth_port, channels={"1": s.game_port}, timeout_s=5, move_interval_ms=100,
                         walk_speed=1500, maps=[{"index": 1, "x": 0, "y": 0, "width": 25600, "height": 25600}])
    c = HeadlessClient(cfg, profile=prof)
    br = LocalBridge(c.handle, on_close=c.close)
    br.connect()
    br.call("login", account="AI_QA_001", password="qa")
    br.call("select_character", name="AI_QA_001")
    br.server = s
    yield br
    br.close()
    s.shutdown()


def _inv(b):
    return {i["slot"]: i["vnum"] for i in b.call("get_inventory")["items"]}


def test_shop_buy_and_sell_like_a_player(b):
    assert b.call("talk_to_npc", vid=SHOP_VID)["window"] == "shop"
    shop = next(w for w in b.call("get_open_windows") if w["name"] == "shop")
    assert shop["npc_vid"] == SHOP_VID and [(i["slot"], i["vnum"], i["price"]) for i in shop["items"]] == \
        [(0, 10, 100), (1, 27001, 50)]
    with pytest.raises(ActionError, match="NOT_ENOUGH_GOLD"):
        b.call("buy_item", slot=0)
    b.call("send_chat", message="/qa gold 500")
    b.call("wait", ms=200)
    r = b.call("buy_item", slot=1)
    assert r["vnum"] == 27001 and r["gold"] == 450 and r["confirmed"]
    new_slot = next(s for s, v in _inv(b).items() if v == 27001 and s != 0)
    r = b.call("sell_item", slot=new_slot)
    assert r["gold"] == 470 and new_slot not in _inv(b)
    b.call("close_window", name="shop")
    b.call("wait", ms=200)
    assert not [w for w in b.call("get_open_windows") if w["name"] == "shop"]


def test_refine_at_blacksmith_shows_cost_then_upgrades(b):
    # bilgi: pencere açılır, onaylamadan kapatılır
    info = b.call("refine_item", slot=2, confirm=False)
    assert (info["confirmed"], info["src_vnum"], info["result_vnum"], info["cost"], info["prob"]) == \
        (False, 10, 11, 300, 100)
    assert info["materials"] == [{"vnum": 30000, "count": 1}]
    with pytest.raises(ActionError, match="MISSING_MATERIALS|NOT_ENOUGH_GOLD"):
        b.call("refine_item", slot=2, npc_vid=SMITH_VID)
    # malzeme (köpek dişi) topla, yang al, sonra bas
    b.call("target", vid=200)
    b.call("attack")
    b.call("wait", ms=3000)
    b.call("pickup", vid=400)
    b.call("send_chat", message="/qa gold 1000")
    b.call("wait", ms=200)
    r = b.call("refine_item", slot=2)            # demirciyi kendisi bulur
    assert r["confirmed"] and r["result"] == "success" and r["slot_vnum"] == 11 and r["gold"] == 700
    assert _inv(b)[2] == 11 and 30000 not in _inv(b).values()
    with pytest.raises(ActionError, match="REFINE_REFUSED"):
        b.call("refine_item", slot=0, npc_vid=SMITH_VID)   # iksir yükseltilemez


def test_skills_equipment_and_drop(b):
    st = b.call("get_player_state")
    assert st["skills"] == {"3": 5}
    with pytest.raises(ActionError, match="SKILL_NOT_LEARNED"):
        b.call("use_skill", slot=4)
    b.call("target", vid=200)
    r = b.call("use_skill", slot=3)
    assert r["skill"] == 3 and r["target_vid"] == 200
    stats = b.server.world.stats
    assert stats["skills_used"] == [(3, 200)] and stats["skill_hits"] == 1
    assert b.call("get_inventory")["equipment"]["body"]["vnum"] == 11200
    r = b.call("unequip_item", wear_slot="body")
    assert _inv(b)[r["slot"]] == 11200 and "body" not in b.call("get_inventory")["equipment"]
    r = b.call("drop_item", slot=0, count=1)
    assert r == {"vnum": 27001, "count": 1}
    b.call("wait", ms=200)
    inv = b.call("get_inventory")["items"]
    assert next(i["count"] for i in inv if i["slot"] == 0) == 2
    assert any(e["vid"] == 410 for e in b.call("get_nearby_entities", type="item"))
