"""Oyuncu davranışları: sahibin ticaret teklifini bekleyip onaylama, oyuncuya gitme, NPC/oyuncu ayrımı."""

import pytest

from qa.engine import behaviours as B
from qa.engine.executor import BehaviourError


class StubCtx:
    """Yalnız bu davranışların kullandığı GameContext yüzeyi."""

    def __init__(self, windows=None, entities=None, script=None):
        self._windows = windows or {}
        self._entities = entities or []
        self.events: list[dict] = []
        self.acts: list[tuple] = []
        self.t = 0
        self.script = script or (lambda ctx: None)   # her beklemede dünyayı ilerletir

    def windows(self):
        return self._windows

    def entities(self, type=None, radius=None, vnum=None):
        return [e for e in self._entities if type is None or e["type"] == type]

    def act(self, cmd, **args):
        self.acts.append((cmd, args))
        if cmd == "trade_accept":
            w = self._windows.get("trade")
            if w and w.get("their_accepted"):
                self._windows.pop("trade")
                self.events.append({"event": "trade_completed", "data": {}})
                return {"completed": True}
        return {}

    def react(self):
        pass

    def now(self):
        return self.t

    def wait(self, ms):
        self.t += ms
        self.script(self)

    def events_since(self, index, name=None):
        return [e for e in self.events[index:] if name is None or e["event"] == name]

    def state(self):
        return {"in_game": True, "x": 0, "y": 0, "map": 1}


def test_trade_wait_and_accept_waits_for_owner_offer_then_accepts():
    trade = {"their_gold": 0, "their_accepted": False, "my_accepted": False}

    def owner(ctx):          # 1,5 sn sonra sahip 500 yang koyup onaylıyor
        if ctx.t >= 1500:
            trade.update(their_gold=500, their_accepted=True)

    ctx = StubCtx(windows={"trade": trade}, script=owner)
    r = B.trade_wait_and_accept(ctx, timeout_ms=10000)
    assert r["completed"] and r["received_gold"] == 500
    assert [a[0] for a in ctx.acts] == ["trade_accept"]


def test_trade_wait_and_accept_times_out_and_cancels():
    ctx = StubCtx(windows={"trade": {"their_accepted": False}})
    with pytest.raises(BehaviourError, match="TRADE_TIMEOUT"):
        B.trade_wait_and_accept(ctx, timeout_ms=2000)
    assert ctx.acts[-1][0] == "trade_cancel"


def test_npc_and_player_are_not_confused():
    ents = [{"vid": 1, "type": "pc", "vnum": 0, "name": "TESTR", "x": 900, "y": 0, "distance": 900},
            {"vid": 2, "type": "npc", "vnum": 20107, "name": "TESTRAt", "x": 50, "y": 0, "distance": 50}]
    ctx = StubCtx(entities=ents)
    with pytest.raises(BehaviourError, match="IS_PLAYER"):
        B.go_to_npc(ctx, name="TESTR")
    with pytest.raises(BehaviourError, match="PLAYER_NOT_VISIBLE"):
        B.go_to_player(ctx, name="Uzaktaki")
