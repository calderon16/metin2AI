"""Deterministik Metin2 simülatörü.

Gerçek QA client + game server zincirinin yerine geçer; bridge protokolünü birebir konuşur.
Amaç, orchestrator/behaviour/oracle/MCP katmanlarını oyun kaynağı olmadan uçtan uca test
edebilmektir. Tüm rastgelelik tek bir seed'li RNG'den gelir ve zaman sadece `wait` ile ilerler,
bu yüzden aynı seed + aynı komut dizisi her zaman aynı sonucu verir.

`faults` ile bilinçli hatalar enjekte edilebilir (bkz. FAULTS) — bug oracle'ın gerçekten
hata yakaladığını doğrulamak için kullanılır.
"""

from __future__ import annotations

import base64
import math
import random
from dataclasses import dataclass, field
from typing import Any

from ..bridge.protocol import PROTOCOL_VERSION, err, ok
from .png import render_png

TICK_MS = 50
PLAYER_SPEED = 500  # birim/sn
MOB_SPEED = 300
MELEE_RANGE = 300
INTERACT_RANGE = 350
PLAYER_ATTACK_MS = 800
MOB_ATTACK_MS = 1500
MOB_RESPAWN_MS = 8000
INVENTORY_SIZE = 45
TRADE_RANGE = 1000
TRADE_MAX_ITEMS = 12
PARTY_MAX = 8
PARTY_EXP_RANGE = 5000
QA_PREFIX = "AI_QA_"
MAP_SIZE = 25600

FAULTS: dict[str, str] = {
    "no_drop": "Moblar hiç item düşürmez",
    "quest_off_by_one": "Köpek avı görevi kabul sonrası ilk kesimi saymaz",
    "potion_no_heal": "İksir tüketilir ama HP artmaz",
    "sell_no_gold": "NPC'ye satışta yang verilmez",
    "syserr_on_equip": "Ekipman giyilince sunucu SYSERR loglar",
    "negative_gold_on_buy": "Satın almada yang kontrolü yapılmaz (yang eksiye düşebilir)",
    "trade_item_dupe": "Trade tamamlanınca veren taraf item'i kaybetmez (item kopyalama)",
    "trade_gold_dupe": "Trade tamamlanınca veren tarafın yangı düşülmez (yang kopyalama)",
    "trade_accept_not_reset": "Onaydan sonra teklif değişince onaylar sıfırlanmaz (dolandırıcılık açığı)",
    "party_exp_dupe": "Party exp paylaşımında her üye tam exp alır",
}

ITEMS: dict[int, dict[str, Any]] = {
    10: {"name": "Kılıç", "type": "weapon", "wear": "weapon", "price": 100, "stack": False, "attack": 15},
    11200: {"name": "Tahta Zırh", "type": "armor", "wear": "body", "price": 200, "stack": False, "defense": 10},
    27001: {"name": "Kırmızı İksir (K)", "type": "potion", "price": 50, "stack": True, "heal": 50},
    30000: {"name": "Köpek Dişi", "type": "material", "price": 5, "stack": True},
}

MOBS: dict[int, dict[str, Any]] = {
    101: {"name": "Yaban Köpeği", "hp": 120, "dmg": (4, 8), "exp": 20, "drops": [(30000, 0.6), (27001, 0.2)]},
    102: {"name": "Kurt", "hp": 220, "dmg": (8, 14), "exp": 45, "drops": [(11200, 0.15), (30000, 0.5)]},
}

NPCS: dict[int, dict[str, Any]] = {
    9001: {"name": "Genel Mağaza Satıcısı", "shop": [27001, 10, 11200]},
    20016: {"name": "Köy Muhafızı", "quest": "dog_hunt"},
}

DOG_HUNT_GOAL = 5
PLAYER_SPAWN = (5000, 5000)
MAP_INDEX = 1

# (vnum, x, y)
NPC_SPAWNS = [(9001, 5400, 5000), (20016, 4600, 5200)]
# (vnum, merkez_x, merkez_y, adet, yarıçap)
MOB_GROUPS = [(101, 8000, 8000, 6, 800), (102, 11000, 6000, 3, 600)]


def dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


@dataclass
class Entity:
    vid: int
    type: str  # monster | npc | item
    vnum: int
    x: float
    y: float
    hp: int = 0
    max_hp: int = 0
    dead: bool = False
    spawn: tuple[float, float] = (0, 0)
    respawn_at: int | None = None
    count: int = 1
    aggro_pid: int | None = None
    next_attack_at: int = 0

    @property
    def name(self) -> str:
        if self.type == "monster":
            return MOBS[self.vnum]["name"]
        if self.type == "npc":
            return NPCS[self.vnum]["name"]
        return ITEMS[self.vnum]["name"]


@dataclass
class Player:
    pid: int
    vid: int
    name: str
    level: int = 10
    exp: int = 0
    hp: int = 0
    sp: int = 100
    max_sp: int = 100
    gold: int = 0
    x: float = PLAYER_SPAWN[0]
    y: float = PLAYER_SPAWN[1]
    map: int = MAP_INDEX
    channel: int = 1
    dead: bool = False
    in_game: bool = False
    dest: tuple[float, float] | None = None
    target_vid: int | None = None
    attacking: bool = False
    next_attack_at: int = 0
    inventory: list[dict[str, int] | None] = field(default_factory=lambda: [None] * INVENTORY_SIZE)
    equipment: dict[str, dict[str, int]] = field(default_factory=dict)
    quests: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def max_hp(self) -> int:
        return 200 + self.level * 20

    @property
    def attack_power(self) -> int:
        w = self.equipment.get("weapon")
        return 10 + self.level * 2 + (ITEMS[w["vnum"]].get("attack", 0) if w else 0)

    @property
    def defense(self) -> int:
        a = self.equipment.get("body")
        return ITEMS[a["vnum"]].get("defense", 0) if a else 0

    def reset(self) -> None:
        self.level, self.exp, self.gold = 10, 0, 0
        self.hp, self.sp = self.max_hp, self.max_sp
        self.x, self.y, self.map = PLAYER_SPAWN[0], PLAYER_SPAWN[1], MAP_INDEX
        self.dead, self.dest, self.target_vid, self.attacking = False, None, None, False
        self.inventory = [None] * INVENTORY_SIZE
        self.equipment = {}
        self.quests = {}

    def count_item(self, vnum: int) -> int:
        return sum(s["count"] for s in self.inventory if s and s["vnum"] == vnum)

    def add_item(self, vnum: int, count: int) -> bool:
        proto = ITEMS[vnum]
        if proto["stack"]:
            for s in self.inventory:
                if s and s["vnum"] == vnum and s["count"] + count <= 200:
                    s["count"] += count
                    return True
            free = [i for i, s in enumerate(self.inventory) if s is None]
            if not free:
                return False
            self.inventory[free[0]] = {"vnum": vnum, "count": count}
            return True
        free = [i for i, s in enumerate(self.inventory) if s is None]
        if len(free) < count:
            return False
        for i in free[:count]:
            self.inventory[i] = {"vnum": vnum, "count": 1}
        return True


@dataclass
class Trade:
    a: Player
    b: Player
    items: dict[int, list[int]] = field(default_factory=dict)      # pid -> envanter slotları
    gold: dict[int, int] = field(default_factory=dict)
    accepted: dict[int, bool] = field(default_factory=dict)
    completed: bool = False
    cancel_reason: str | None = None

    def partner(self, p: Player) -> Player:
        return self.b if p is self.a else self.a


@dataclass
class Party:
    leader: int                      # pid
    members: list[int] = field(default_factory=list)


class SimWorld:
    """Tek harita, birden fazla oyuncu barındırabilen sunucu tarafı."""

    def __init__(self, seed: int = 0, faults: list[str] | None = None, password: str = "qa"):
        self.password = password
        self.clients: list["SimClient"] = []
        self.players: dict[str, Player] = {}
        self.reset(seed, faults or [])

    # ------------------------------------------------------------------ durum
    def reset(self, seed: int, faults: list[str] | None = None) -> None:
        unknown = set(faults or []) - set(FAULTS)
        if unknown:
            raise ValueError(f"Bilinmeyen fault: {sorted(unknown)}")
        self.seed = seed
        self.faults = set(faults or [])
        self.rng = random.Random(seed)
        self.time_ms = 0
        self._vid = 1000
        self.entities: dict[int, Entity] = {}
        self.server_events: list[dict[str, Any]] = []
        self._event_seq = 0
        self._assert_active: set[tuple[int, str]] = set()
        self.trades: dict[int, Trade] = {}          # pid -> trade (iki taraf da aynı nesneyi görür)
        self.parties: dict[int, Party] = {}         # pid -> party
        self.party_invites: dict[int, int] = {}     # davet edilen pid -> davet eden pid
        for vnum, x, y in NPC_SPAWNS:
            self._spawn("npc", vnum, x, y)
        for vnum, cx, cy, n, r in MOB_GROUPS:
            for _ in range(n):
                a = self.rng.uniform(0, 2 * math.pi)
                d = self.rng.uniform(0, r)
                x, y = round(cx + math.cos(a) * d), round(cy + math.sin(a) * d)
                e = self._spawn("monster", vnum, x, y)
                e.hp = e.max_hp = MOBS[vnum]["hp"]
        # Oyuncular dünyada kalır ama oyundan çıkarılır (yeniden login gerekir)
        for p in self.players.values():
            p.reset()
            p.in_game = False
            p.vid = self._next_vid()
        for c in self.clients:
            c.reset_session()

    def _next_vid(self) -> int:
        self._vid += 1
        return self._vid

    def _spawn(self, typ: str, vnum: int, x: float, y: float, count: int = 1) -> Entity:
        e = Entity(vid=self._next_vid(), type=typ, vnum=vnum, x=x, y=y, spawn=(x, y), count=count)
        self.entities[e.vid] = e
        return e

    def get_player(self, name: str) -> Player:
        if name not in self.players:
            p = Player(pid=len(self.players) + 1, vid=self._next_vid(), name=name)
            p.reset()
            self.players[name] = p
        return self.players[name]

    def player_by_vid(self, vid: int) -> Player | None:
        return next((p for p in self.players.values() if p.vid == vid and p.in_game), None)

    # ----------------------------------------------------------- olay/log
    def server_event(self, typ: str, name: str, p: Player | None = None, **data: Any) -> None:
        self._event_seq += 1
        ev = {"seq": self._event_seq, "t": self.time_ms, "type": typ, "name": name, "data": data}
        if p is not None:
            ev["pid"] = p.pid
            ev["player"] = p.name
        self.server_events.append(ev)

    def syserr(self, message: str, p: Player | None = None) -> None:
        self.server_event("syserr", "SYSERR", p, message=message)

    def qa_assert(self, p: Player, cond: bool, name: str, **data: Any) -> None:
        key = (p.pid, name)
        if not cond and key not in self._assert_active:
            self._assert_active.add(key)
            self.server_event("assert_fail", name, p, **data)
        elif cond:
            self._assert_active.discard(key)

    def client_event(self, p: Player, event: str, **data: Any) -> None:
        for c in self.clients:
            if c.player is p:
                c.push_event(event, data)

    def system_message(self, p: Player, text: str) -> None:
        for c in self.clients:
            if c.player is p:
                c.push_message(text)

    # ------------------------------------------------------------ simülasyon
    def advance(self, ms: int) -> None:
        end = self.time_ms + max(0, int(ms))
        while self.time_ms < end:
            self.time_ms += TICK_MS
            self._tick()

    def _tick(self) -> None:
        step = PLAYER_SPEED * TICK_MS / 1000.0
        for p in self.players.values():
            if not p.in_game or p.dead:
                continue
            if p.dest:
                dx, dy = p.dest[0] - p.x, p.dest[1] - p.y
                d = math.hypot(dx, dy)
                if d <= step:
                    p.x, p.y = p.dest
                    p.dest = None
                else:
                    p.x += dx / d * step
                    p.y += dy / d * step
            if p.attacking:
                self._player_combat(p)
        for e in list(self.entities.values()):
            if e.type == "monster":
                self._monster_tick(e)
        for p in self.players.values():
            if p.in_game:
                self.qa_assert(p, p.gold >= 0, "PLAYER_NEGATIVE_GOLD", gold=p.gold)
                self.qa_assert(p, p.hp <= p.max_hp, "PLAYER_HP_OVER_MAX", hp=p.hp, max_hp=p.max_hp)
                self.qa_assert(
                    p, all(s is None or s["count"] > 0 for s in p.inventory), "ITEM_ZERO_COUNT"
                )

    def _player_combat(self, p: Player) -> None:
        t = self.entities.get(p.target_vid) if p.target_vid else None
        if t is None or t.dead or t.type != "monster":
            p.attacking = False
            return
        if dist(p.x, p.y, t.x, t.y) > MELEE_RANGE or self.time_ms < p.next_attack_at:
            return
        p.next_attack_at = self.time_ms + PLAYER_ATTACK_MS
        dmg = max(1, round(p.attack_power * self.rng.uniform(0.8, 1.2)))
        t.hp -= dmg
        t.aggro_pid = p.pid
        self.client_event(p, "damage_dealt", vid=t.vid, damage=dmg, target_hp=max(0, t.hp))
        if t.hp <= 0:
            self._kill_monster(p, t)

    def _kill_monster(self, p: Player, m: Entity) -> None:
        m.dead, m.hp, m.aggro_pid = True, 0, None
        m.respawn_at = self.time_ms + MOB_RESPAWN_MS
        p.attacking = False
        p.target_vid = None
        proto = MOBS[m.vnum]
        self.server_event("event", "MOB_KILL", p, vnum=m.vnum, vid=m.vid)
        self.client_event(p, "entity_dead", vid=m.vid, vnum=m.vnum)
        self._give_kill_exp(p, proto["exp"])
        if "no_drop" not in self.faults:
            for vnum, chance in proto["drops"]:
                if self.rng.random() < chance:
                    ox, oy = self.rng.randint(-80, 80), self.rng.randint(-80, 80)
                    it = self._spawn("item", vnum, m.x + ox, m.y + oy)
                    self.server_event("event", "ITEM_DROP", p, vnum=vnum, vid=it.vid)
                    self.client_event(p, "item_dropped", vid=it.vid, vnum=vnum)
        q = p.quests.get("dog_hunt")
        if m.vnum == 101 and q and q["state"] == "active":
            if "quest_off_by_one" in self.faults and not q.get("_skipped"):
                q["_skipped"] = True
            else:
                q["progress"] += 1
            self.server_event("event", "QUEST_PROGRESS", p, quest="dog_hunt", progress=q["progress"])
            if q["progress"] >= q["goal"]:
                q["state"] = "ready"
                self.system_message(p, "Görev tamamlandı: Köy Muhafızı'na dön.")

    def player_by_pid(self, pid: int) -> Player | None:
        return next((p for p in self.players.values() if p.pid == pid), None)

    def _give_kill_exp(self, p: Player, exp: int) -> None:
        party = self.parties.get(p.pid)
        if party is None:
            self._gain_exp(p, exp)
            return
        near = [m for m in (self.player_by_pid(pid) for pid in party.members)
                if m and m.in_game and not m.dead and m.map == p.map and dist(m.x, m.y, p.x, p.y) <= PARTY_EXP_RANGE]
        share = exp if "party_exp_dupe" in self.faults else max(1, exp // len(near))
        for m in near:
            self._gain_exp(m, share)
        self.server_event("event", "PARTY_EXP", p, total=exp, share=share, members=len(near))

    # ------------------------------------------------------------ trade
    def trade_view(self, p: Player) -> dict[str, Any] | None:
        t = self.trades.get(p.pid)
        if t is None:
            return None
        o = t.partner(p)

        def items(pl: Player) -> list[dict[str, Any]]:
            out = []
            for slot in t.items.get(pl.pid, []):
                it = pl.inventory[slot]
                if it:
                    out.append({"slot": slot, "vnum": it["vnum"], "count": it["count"], "name": ITEMS[it["vnum"]]["name"]})
            return out

        return {"partner_vid": o.vid, "partner_name": o.name, "my_items": items(p), "their_items": items(o),
                "my_gold": t.gold.get(p.pid, 0), "their_gold": t.gold.get(o.pid, 0),
                "my_accepted": t.accepted.get(p.pid, False), "their_accepted": t.accepted.get(o.pid, False)}

    def trade_notify(self, t: Trade, event: str, **data: Any) -> None:
        for pl in (t.a, t.b):
            self.client_event(pl, event, **data)

    def trade_changed(self, t: Trade) -> None:
        if "trade_accept_not_reset" not in self.faults:
            t.accepted = {}
        self.trade_notify(t, "trade_updated")

    def trade_cancel(self, t: Trade, reason: str, by: Player | None = None) -> None:
        t.cancel_reason = reason
        for pl in (t.a, t.b):
            self.trades.pop(pl.pid, None)
        self.trade_notify(t, "trade_cancelled", reason=reason)
        self.server_event("event", "TRADE_CANCEL", by, reason=reason, a=t.a.name, b=t.b.name)

    def trade_try_complete(self, t: Trade) -> None:
        if not (t.accepted.get(t.a.pid) and t.accepted.get(t.b.pid)):
            return
        # Her iki tarafın yang ve envanter yeri kontrolü
        for giver in (t.a, t.b):
            if t.gold.get(giver.pid, 0) > giver.gold:
                self.trade_cancel(t, "NOT_ENOUGH_GOLD", giver)
                return
        for recv in (t.a, t.b):
            giver = t.partner(recv)
            incoming = len(t.items.get(giver.pid, []))
            free = sum(1 for s in recv.inventory if s is None) + len(t.items.get(recv.pid, []))
            if incoming > free:
                for pl in (t.a, t.b):
                    self.system_message(pl, "Envanterde yeterli yer yok.")
                self.trade_cancel(t, "INVENTORY_FULL", recv)
                return
        moved: dict[int, list[dict[str, int]]] = {}
        for giver in (t.a, t.b):
            out = []
            for slot in t.items.get(giver.pid, []):
                it = giver.inventory[slot]
                if it is None:
                    continue
                out.append(dict(it))
                if "trade_item_dupe" not in self.faults:
                    giver.inventory[slot] = None
            moved[giver.pid] = out
        for giver in (t.a, t.b):
            recv = t.partner(giver)
            for it in moved[giver.pid]:
                recv.add_item(it["vnum"], it["count"])
            g = t.gold.get(giver.pid, 0)
            if "trade_gold_dupe" not in self.faults:
                giver.gold -= g
            recv.gold += g
        t.completed = True
        for pl in (t.a, t.b):
            self.trades.pop(pl.pid, None)
        self.server_event("event", "TRADE_COMPLETE", t.a, partner=t.b.name,
                          a_items=[i["vnum"] for i in moved[t.a.pid]], b_items=[i["vnum"] for i in moved[t.b.pid]],
                          a_gold=t.gold.get(t.a.pid, 0), b_gold=t.gold.get(t.b.pid, 0))
        self.trade_notify(t, "trade_completed")

    # ------------------------------------------------------------ party
    def party_view(self, p: Player) -> dict[str, Any]:
        party = self.parties.get(p.pid)
        if party is None:
            return {"in_party": False, "members": []}
        members = [m for m in (self.player_by_pid(pid) for pid in party.members) if m]
        leader = self.player_by_pid(party.leader)
        return {"in_party": True, "leader_vid": leader.vid if leader else None,
                "leader_name": leader.name if leader else None, "is_leader": party.leader == p.pid,
                "members": [{"vid": m.vid, "name": m.name, "level": m.level, "online": m.in_game} for m in members]}

    def party_remove(self, p: Player, reason: str) -> None:
        party = self.parties.pop(p.pid, None)
        if party is None:
            return
        party.members.remove(p.pid)
        self.client_event(p, "party_left", reason=reason)
        self.server_event("event", "PARTY_LEAVE", p, reason=reason)
        if party.leader == p.pid or len(party.members) < 2:
            # Lider ayrılırsa ya da tek kişi kalırsa party dağılır
            for pid in list(party.members):
                self.parties.pop(pid, None)
                m = self.player_by_pid(pid)
                if m:
                    self.client_event(m, "party_left", reason="DISBANDED")
            self.server_event("event", "PARTY_DISBAND", p)
        else:
            for pid in party.members:
                m = self.player_by_pid(pid)
                if m:
                    self.client_event(m, "party_updated", members=len(party.members))

    def _gain_exp(self, p: Player, exp: int) -> None:
        p.exp += exp
        while p.exp >= p.level * 100:
            p.exp -= p.level * 100
            p.level += 1
            p.hp = p.max_hp
            self.server_event("event", "LEVEL_UP", p, level=p.level)
            self.client_event(p, "level_up", level=p.level)

    def _monster_tick(self, m: Entity) -> None:
        if m.dead:
            if m.respawn_at is not None and self.time_ms >= m.respawn_at:
                m.dead, m.hp, m.respawn_at = False, m.max_hp, None
                m.x, m.y = m.spawn
            return
        if m.aggro_pid is None:
            return
        p = next((pl for pl in self.players.values() if pl.pid == m.aggro_pid), None)
        if p is None or not p.in_game or p.dead:
            m.aggro_pid = None
            return
        d = dist(m.x, m.y, p.x, p.y)
        if d > 3000:  # takibi bırak
            m.aggro_pid = None
            return
        if d > 150:
            step = MOB_SPEED * TICK_MS / 1000.0
            m.x += (p.x - m.x) / d * min(step, d - 150)
            m.y += (p.y - m.y) / d * min(step, d - 150)
        elif self.time_ms >= m.next_attack_at:
            m.next_attack_at = self.time_ms + MOB_ATTACK_MS
            lo, hi = MOBS[m.vnum]["dmg"]
            dmg = max(1, self.rng.randint(lo, hi) - p.defense // 2)
            p.hp -= dmg
            self.client_event(p, "damage_taken", vid=m.vid, damage=dmg, hp=max(0, p.hp))
            if p.hp <= 0:
                p.hp, p.dead, p.attacking, p.dest = 0, True, False, None
                self.server_event("event", "PLAYER_DEAD", p, killer_vnum=m.vnum)
                self.client_event(p, "player_dead", killer_vid=m.vid)
                for e in self.entities.values():
                    if e.aggro_pid == p.pid:
                        e.aggro_pid = None


class SimClient:
    """Tek istemci bağlantısı: login durumu, açık pencereler, mesajlar."""

    CAPABILITIES = ["sim_control", "server_events", "screenshot"]

    def __init__(self, world: SimWorld):
        self.world = world
        world.clients.append(self)
        self.reset_session()

    def reset_session(self) -> None:
        self.account: str | None = None
        self.player: Player | None = None
        self.windows: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.client_log: list[dict[str, Any]] = []
        self._msg_seq = 0
        self._log_seq = 0
        self._pending_events: list[dict[str, Any]] = []

    def close(self) -> None:
        if self.player:
            if self.player.in_game:
                self._leave_cleanup(self.player)
            self.player.in_game = False
        if self in self.world.clients:
            self.world.clients.remove(self)

    # ------------------------------------------------------------- yardımcı
    def push_event(self, event: str, data: dict[str, Any]) -> None:
        self._pending_events.append({"event": event, "t": self.world.time_ms, "data": data})

    def push_message(self, text: str) -> None:
        self._msg_seq += 1
        self.messages.append({"seq": self._msg_seq, "t": self.world.time_ms, "text": text})

    def log(self, text: str) -> None:
        self._log_seq += 1
        self.client_log.append({"seq": self._log_seq, "t": self.world.time_ms, "text": text})

    def handle(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        req_id = msg.get("id")
        cmd = msg.get("cmd", "")
        args = msg.get("args") or {}
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            resp = err(req_id, self.world.time_ms, "UNKNOWN_COMMAND", f"Bilinmeyen komut: {cmd}")
        else:
            try:
                resp = ok(req_id, self.world.time_ms, fn(**args))
            except SimError as e:
                self.log(f"{cmd} reddedildi: {e.code} {e.message}")
                resp = err(req_id, self.world.time_ms, e.code, e.message)
            except TypeError as e:
                resp = err(req_id, self.world.time_ms, "BAD_ARGS", str(e))
            resp["t"] = self.world.time_ms
        out, self._pending_events = self._pending_events, []
        return out + [resp]

    def _need_game(self) -> Player:
        if self.player is None or not self.player.in_game:
            raise SimError("NOT_IN_GAME", "Karakter oyunda değil")
        return self.player

    def _need_alive(self) -> Player:
        p = self._need_game()
        if p.dead:
            raise SimError("DEAD", "Karakter ölü")
        return p

    def _need_free(self) -> Player:
        """Trade açıkken item/dükkan/NPC işlemleri yapılamaz (Metin2 davranışı)."""
        p = self._need_alive()
        if p.pid in self.world.trades:
            raise SimError("IN_TRADE", "Ticaret sırasında bu işlem yapılamaz")
        return p

    def _entity(self, vid: int) -> Entity:
        e = self.world.entities.get(int(vid))
        if e is None:
            raise SimError("NO_ENTITY", f"VID {vid} bulunamadı")
        return e

    def _slot(self, p: Player, slot: int) -> dict[str, int]:
        if not 0 <= slot < INVENTORY_SIZE or p.inventory[slot] is None:
            raise SimError("EMPTY_SLOT", f"Slot {slot} boş")
        return p.inventory[slot]

    def _in_range(self, p: Player, e: Entity, r: float) -> None:
        if dist(p.x, p.y, e.x, e.y) > r:
            raise SimError("OUT_OF_RANGE", f"{e.name} çok uzakta")

    # ------------------------------------------------------------ durum komutları
    def cmd_hello(self) -> dict[str, Any]:
        return {"client": "metin2_qa_sim", "protocol": PROTOCOL_VERSION, "capabilities": self.CAPABILITIES}

    def cmd_get_player_state(self) -> dict[str, Any]:
        p = self.player
        if p is None:
            return {"in_game": False, "logged_in": self.account is not None}
        return {
            "in_game": p.in_game, "logged_in": True, "name": p.name, "vid": p.vid, "level": p.level,
            "exp": p.exp, "hp": p.hp, "max_hp": p.max_hp, "sp": p.sp, "max_sp": p.max_sp,
            "gold": p.gold, "x": round(p.x), "y": round(p.y), "map": p.map, "channel": p.channel,
            "dead": p.dead, "moving": p.dest is not None, "attacking": p.attacking,
            "target_vid": p.target_vid,
        }

    def cmd_get_inventory(self) -> dict[str, Any]:
        p = self._need_game()
        slots = [
            {"slot": i, "vnum": s["vnum"], "count": s["count"], "name": ITEMS[s["vnum"]]["name"]}
            for i, s in enumerate(p.inventory) if s
        ]
        eq = {k: {"vnum": v["vnum"], "name": ITEMS[v["vnum"]]["name"]} for k, v in p.equipment.items()}
        return {"size": INVENTORY_SIZE, "items": slots, "equipment": eq}

    def cmd_get_nearby_entities(self, radius: int = 5000, type: str | None = None,
                                vnum: int | None = None) -> list[dict[str, Any]]:
        p = self._need_game()
        out = []
        for e in self.world.entities.values():
            if e.dead and e.type == "monster":
                continue
            if type and e.type != type or vnum is not None and e.vnum != vnum:
                continue
            d = dist(p.x, p.y, e.x, e.y)
            if d > radius:
                continue
            row = {"vid": e.vid, "type": e.type, "vnum": e.vnum, "name": e.name,
                   "x": round(e.x), "y": round(e.y), "distance": round(d)}
            if e.type == "monster":
                row["hp_pct"] = round(100 * e.hp / e.max_hp)
            if e.type == "item":
                row["count"] = e.count
            out.append(row)
        if type in (None, "pc"):
            for o in self.world.players.values():
                if o is p or not o.in_game or o.map != p.map:
                    continue
                d = dist(p.x, p.y, o.x, o.y)
                if d <= radius:
                    out.append({"vid": o.vid, "type": "pc", "vnum": 0, "name": o.name,
                                "x": round(o.x), "y": round(o.y), "distance": round(d)})
        out.sort(key=lambda r: (r["distance"], r["vid"]))
        return out

    def cmd_get_target(self) -> dict[str, Any] | None:
        p = self._need_game()
        if not p.target_vid or p.target_vid not in self.world.entities:
            return None
        e = self.world.entities[p.target_vid]
        return {"vid": e.vid, "type": e.type, "vnum": e.vnum, "name": e.name, "dead": e.dead,
                "hp_pct": round(100 * e.hp / e.max_hp) if e.max_hp else None}

    def cmd_get_open_windows(self) -> list[dict[str, Any]]:
        out = [{"name": k, **v} for k, v in self.windows.items()]
        if self.player is not None:
            tv = self.world.trade_view(self.player)
            if tv:
                out.append({"name": "trade", **tv})
            inviter = self.world.party_invites.get(self.player.pid)
            if inviter is not None:
                leader = self.world.player_by_pid(inviter)
                out.append({"name": "party_invite", "leader_vid": leader.vid if leader else None,
                            "leader_name": leader.name if leader else None})
        return out

    def cmd_get_party(self) -> dict[str, Any]:
        return self.world.party_view(self._need_game())

    def cmd_get_quest_state(self) -> dict[str, Any]:
        p = self._need_game()
        return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in p.quests.items()}

    def cmd_get_system_messages(self, since: int = 0) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["seq"] > since]

    def cmd_get_client_log(self, since: int = 0) -> list[dict[str, Any]]:
        return [m for m in self.client_log if m["seq"] > since]

    def cmd_wait(self, ms: int) -> dict[str, Any]:
        self.world.advance(int(ms))
        return {}

    def cmd_screenshot(self) -> dict[str, Any]:
        p = self._need_game()
        png = render_png(self.world, p)
        return {"format": "png", "base64": base64.b64encode(png).decode()}

    # ------------------------------------------------------------ sim kontrol
    def cmd_sim_reset(self, seed: int, faults: list[str] | None = None) -> dict[str, Any]:
        try:
            self.world.reset(int(seed), faults if faults is not None else sorted(self.world.faults))
        except ValueError as e:
            raise SimError("BAD_ARGS", str(e)) from e
        return {"seed": self.world.seed, "faults": sorted(self.world.faults)}

    def cmd_qa_server_events(self, since: int = 0) -> list[dict[str, Any]]:
        return [e for e in self.world.server_events if e["seq"] > since]

    # ------------------------------------------------------------ oturum
    def cmd_login(self, account: str, password: str) -> dict[str, Any]:
        if password != self.world.password:
            raise SimError("LOGIN_FAILED", "Hatalı hesap veya şifre")
        self.account = account
        p = self.world.get_player(account)
        self.log(f"login {account}")
        return {"characters": [{"index": 0, "name": p.name, "level": p.level, "job": "warrior"}]}

    def cmd_select_character(self, name: str | None = None, index: int | None = None) -> dict[str, Any]:
        if self.account is None:
            raise SimError("NOT_LOGGED_IN", "Önce login olunmalı")
        if name not in (None, self.account) or index not in (None, 0):
            raise SimError("NO_CHARACTER", "Karakter bulunamadı")
        p = self.world.get_player(self.account)
        if any(c.player is p for c in self.world.clients if c is not self):
            raise SimError("ALREADY_ONLINE", "Karakter başka bir oturumda açık")
        p.in_game = True
        self.player = p
        self.world.advance(1000)  # yükleme ekranı
        self.world.server_event("event", "LOGIN", p, map=p.map, channel=p.channel)
        self.push_event("map_loaded", {"map": p.map})
        self.log(f"map loaded {p.map}")
        return {"name": p.name, "map": p.map}

    def _leave_cleanup(self, p: Player) -> None:
        t = self.world.trades.get(p.pid)
        if t:
            self.world.trade_cancel(t, "PARTNER_LEFT", p)
        self.world.party_invites.pop(p.pid, None)

    def cmd_logout(self) -> dict[str, Any]:
        p = self._need_game()
        self._leave_cleanup(p)
        p.in_game, p.attacking, p.dest = False, False, None
        self.world.server_event("event", "LOGOUT", p)
        self.windows.clear()
        self.player = None
        self.account = None
        return {}

    def cmd_change_channel(self, channel: int) -> dict[str, Any]:
        p = self._need_alive()
        if not 1 <= int(channel) <= 4:
            raise SimError("BAD_CHANNEL", "Kanal 1-4 arası olmalı")
        self._leave_cleanup(p)
        p.channel, p.attacking, p.target_vid, p.dest = int(channel), False, None, None
        self.windows.clear()
        self.world.advance(2000)
        self.world.server_event("event", "CHANNEL_CHANGE", p, channel=p.channel)
        self.push_event("map_loaded", {"map": p.map, "channel": p.channel})
        return {"channel": p.channel}

    # ------------------------------------------------------------ hareket/savaş
    def cmd_move_to(self, x: float, y: float) -> dict[str, Any]:
        p = self._need_alive()
        if not (0 <= x <= MAP_SIZE and 0 <= y <= MAP_SIZE):
            raise SimError("BAD_POSITION", "Harita dışı")
        p.dest = (float(x), float(y))
        p.attacking = False
        return {}

    def cmd_target(self, vid: int) -> dict[str, Any]:
        p = self._need_alive()
        e = self.world.entities.get(int(vid))
        if e is not None and e.dead:
            raise SimError("TARGET_DEAD", "Hedef ölü")
        if e is None and self.world.player_by_vid(int(vid)) is None:
            raise SimError("NO_ENTITY", f"VID {vid} bulunamadı")
        p.target_vid = int(vid)
        return {}

    def cmd_attack(self) -> dict[str, Any]:
        p = self._need_alive()
        t = self.world.entities.get(p.target_vid) if p.target_vid else None
        if t is None or t.type != "monster" or t.dead:
            raise SimError("NO_TARGET", "Saldırılacak hedef yok")
        self._in_range(p, t, MELEE_RANGE)
        p.dest = None
        p.attacking = True
        return {}

    def cmd_stop_attack(self) -> dict[str, Any]:
        self._need_game().attacking = False
        return {}

    def cmd_use_skill(self, slot: int) -> dict[str, Any]:
        p = self._need_alive()
        if p.sp < 20:
            raise SimError("NOT_ENOUGH_SP", "Yetersiz SP")
        t = self.world.entities.get(p.target_vid) if p.target_vid else None
        if t is None or t.type != "monster" or t.dead:
            raise SimError("NO_TARGET", "Hedef yok")
        self._in_range(p, t, MELEE_RANGE)
        p.sp -= 20
        dmg = round(p.attack_power * 1.8)
        t.hp -= dmg
        t.aggro_pid = p.pid
        self.push_event("damage_dealt", {"vid": t.vid, "damage": dmg, "skill": slot, "target_hp": max(0, t.hp)})
        if t.hp <= 0:
            self.world._kill_monster(p, t)
        return {"damage": dmg}

    def cmd_respawn(self, here: bool = False) -> dict[str, Any]:
        p = self._need_game()
        if not p.dead:
            raise SimError("NOT_DEAD", "Karakter ölü değil")
        p.dead, p.hp = False, p.max_hp // 2
        if not here:
            p.x, p.y = PLAYER_SPAWN
        self.world.server_event("event", "RESPAWN", p, here=here)
        return {}

    # ------------------------------------------------------------ item
    def cmd_pickup(self, vid: int) -> dict[str, Any]:
        p = self._need_free()
        e = self._entity(vid)
        if e.type != "item":
            raise SimError("NOT_ITEM", "Bu bir item değil")
        self._in_range(p, e, MELEE_RANGE)
        if not p.add_item(e.vnum, e.count):
            self.push_message("Envanterde yeterli yer yok.")
            raise SimError("INVENTORY_FULL", "Envanter dolu")
        del self.world.entities[e.vid]
        self.world.server_event("event", "ITEM_PICKUP", p, vnum=e.vnum, count=e.count)
        return {"vnum": e.vnum, "count": e.count}

    def cmd_use_item(self, slot: int) -> dict[str, Any]:
        p = self._need_free()
        s = self._slot(p, slot)
        proto = ITEMS[s["vnum"]]
        if proto.get("wear"):
            return self.cmd_equip_item(slot)
        if proto["type"] != "potion":
            raise SimError("CANNOT_USE", f"{proto['name']} kullanılamaz")
        vnum = s["vnum"]
        s["count"] -= 1
        if s["count"] <= 0:
            p.inventory[slot] = None
        if "potion_no_heal" not in self.world.faults:
            p.hp = min(p.max_hp, p.hp + proto["heal"])
        self.world.server_event("event", "ITEM_USE", p, vnum=vnum, hp=p.hp)
        return {"hp": p.hp}

    def cmd_equip_item(self, slot: int) -> dict[str, Any]:
        p = self._need_free()
        s = self._slot(p, slot)
        wear = ITEMS[s["vnum"]].get("wear")
        if not wear:
            raise SimError("NOT_EQUIPPABLE", "Giyilemez")
        old = p.equipment.get(wear)
        p.equipment[wear] = s
        p.inventory[slot] = old
        self.world.server_event("event", "EQUIP", p, vnum=s["vnum"], wear=wear)
        if "syserr_on_equip" in self.world.faults:
            self.world.syserr(f"CHARACTER::EquipItem: invalid wear flag for vnum {s['vnum']}", p)
        return {"wear": wear}

    def cmd_unequip_item(self, wear_slot: str) -> dict[str, Any]:
        p = self._need_free()
        it = p.equipment.get(wear_slot)
        if not it:
            raise SimError("EMPTY_SLOT", "Ekipman slotu boş")
        free = [i for i, s in enumerate(p.inventory) if s is None]
        if not free:
            raise SimError("INVENTORY_FULL", "Envanter dolu")
        p.inventory[free[0]] = p.equipment.pop(wear_slot)
        return {"slot": free[0]}

    def cmd_drop_item(self, slot: int, count: int | None = None) -> dict[str, Any]:
        p = self._need_free()
        s = self._slot(p, slot)
        n = s["count"] if count is None else int(count)
        if not 0 < n <= s["count"]:
            raise SimError("BAD_COUNT", "Geçersiz adet")
        s["count"] -= n
        if s["count"] == 0:
            p.inventory[slot] = None
        e = self.world._spawn("item", s["vnum"], p.x + 50, p.y + 50, count=n)
        self.world.server_event("event", "ITEM_DROP_BY_PLAYER", p, vnum=s["vnum"], count=n)
        return {"vid": e.vid}

    # ------------------------------------------------------------ NPC / shop / dialog
    def cmd_talk_to_npc(self, vid: int) -> dict[str, Any]:
        p = self._need_free()
        e = self._entity(vid)
        if e.type != "npc":
            raise SimError("NOT_NPC", "Bu bir NPC değil")
        self._in_range(p, e, INTERACT_RANGE)
        proto = NPCS[e.vnum]
        if "shop" in proto:
            items = [{"slot": i, "vnum": v, "name": ITEMS[v]["name"], "price": ITEMS[v]["price"]}
                     for i, v in enumerate(proto["shop"])]
            self.windows["shop"] = {"npc_vid": e.vid, "npc_vnum": e.vnum, "items": items}
            self.push_event("window_opened", {"name": "shop"})
            return {"window": "shop"}
        self._open_quest_dialog(p, e)
        return {"window": "dialog"}

    def _open_quest_dialog(self, p: Player, e: Entity) -> None:
        q = p.quests.get("dog_hunt")
        if q is None:
            text = f"Köyün etrafında yaban köpekleri çoğaldı. {DOG_HUNT_GOAL} tanesini avlar mısın?"
            options = ["Kabul ediyorum", "Şimdi değil"]
        elif q["state"] == "active":
            text = f"Hâlâ {q['goal'] - q['progress']} köpek kaldı."
            options = ["Tamam"]
        elif q["state"] == "ready":
            text = "Harika iş! İşte ödülün."
            options = ["Ödülü al"]
        else:
            text = "Tekrar teşekkürler, yolcu."
            options = ["Kapat"]
        self.windows["dialog"] = {"npc_vid": e.vid, "npc_vnum": e.vnum, "text": text, "options": options}
        self.push_event("window_opened", {"name": "dialog"})

    def cmd_select_dialog(self, index: int) -> dict[str, Any]:
        p = self._need_alive()
        d = self.windows.get("dialog")
        if not d:
            raise SimError("NO_DIALOG", "Açık dialog yok")
        if not 0 <= index < len(d["options"]):
            raise SimError("BAD_OPTION", "Geçersiz seçenek")
        choice = d["options"][index]
        del self.windows["dialog"]
        q = p.quests.get("dog_hunt")
        if choice == "Kabul ediyorum":
            p.quests["dog_hunt"] = {"state": "active", "progress": 0, "goal": DOG_HUNT_GOAL}
            self.world.server_event("event", "QUEST_START", p, quest="dog_hunt")
            self.push_message("Yeni görev: Köpek Avı")
        elif choice == "Ödülü al" and q and q["state"] == "ready":
            if not p.add_item(27001, 5):
                self.push_message("Envanterde yeterli yer yok.")
                raise SimError("INVENTORY_FULL", "Envanter dolu")
            p.gold += 500
            q["state"] = "done"
            self.world.server_event("event", "QUEST_REWARD", p, quest="dog_hunt", gold=500, item=27001)
            self.push_message("Ödül alındı: 5x Kırmızı İksir (K), 500 Yang")
        return {"selected": choice}

    def cmd_close_window(self, name: str) -> dict[str, Any]:
        if name == "trade":
            return self.cmd_trade_cancel()
        if name == "party_invite":
            return self.cmd_party_answer(False)
        self.windows.pop(name, None)
        return {}

    # ------------------------------------------------------------ trade (exchange)
    def _trade(self, p: Player) -> Trade:
        t = self.world.trades.get(p.pid)
        if t is None:
            raise SimError("NO_TRADE", "Açık ticaret yok")
        return t

    def cmd_trade_request(self, vid: int) -> dict[str, Any]:
        p = self._need_alive()
        o = self.world.player_by_vid(int(vid))
        if o is None or o is p:
            raise SimError("NO_ENTITY", f"Oyuncu bulunamadı (VID {vid})")
        if o.dead or o.map != p.map or dist(p.x, p.y, o.x, o.y) > TRADE_RANGE:
            raise SimError("OUT_OF_RANGE", f"{o.name} çok uzakta")
        if p.pid in self.world.trades or o.pid in self.world.trades:
            raise SimError("ALREADY_TRADING", "Taraflardan biri zaten ticarette")
        t = Trade(p, o)
        self.world.trades[p.pid] = self.world.trades[o.pid] = t
        self.world.client_event(p, "trade_started", partner_vid=o.vid, partner_name=o.name)
        self.world.client_event(o, "trade_started", partner_vid=p.vid, partner_name=p.name)
        self.world.server_event("event", "TRADE_START", p, partner=o.name)
        return {"partner": o.name}

    def cmd_trade_add_item(self, slot: int) -> dict[str, Any]:
        p = self._need_alive()
        t = self._trade(p)
        self._slot(p, slot)
        mine = t.items.setdefault(p.pid, [])
        if slot in mine:
            raise SimError("ALREADY_ADDED", "Item zaten eklendi")
        if len(mine) >= TRADE_MAX_ITEMS:
            raise SimError("TRADE_FULL", "Ticaret penceresi dolu")
        mine.append(slot)
        self.world.trade_changed(t)
        return {}

    def cmd_trade_set_gold(self, amount: int) -> dict[str, Any]:
        p = self._need_alive()
        t = self._trade(p)
        amount = int(amount)
        if amount < 0 or amount > p.gold:
            self.push_message("Yeterli Yang yok.")
            raise SimError("NOT_ENOUGH_GOLD", "Yetersiz yang")
        t.gold[p.pid] = amount
        self.world.trade_changed(t)
        return {}

    def cmd_trade_accept(self) -> dict[str, Any]:
        p = self._need_alive()
        t = self._trade(p)
        t.accepted[p.pid] = True
        self.world.trade_notify(t, "trade_updated")
        self.world.trade_try_complete(t)
        return {"completed": t.completed, "cancelled": t.cancel_reason}

    def cmd_trade_cancel(self) -> dict[str, Any]:
        p = self._need_game()
        self.world.trade_cancel(self._trade(p), "CANCELLED", p)
        return {}

    # ------------------------------------------------------------ party
    def cmd_party_invite(self, vid: int) -> dict[str, Any]:
        p = self._need_alive()
        o = self.world.player_by_vid(int(vid))
        if o is None or o is p:
            raise SimError("NO_ENTITY", f"Oyuncu bulunamadı (VID {vid})")
        party = self.world.parties.get(p.pid)
        if party and party.leader != p.pid:
            raise SimError("NOT_LEADER", "Sadece lider davet edebilir")
        if o.pid in self.world.parties:
            raise SimError("ALREADY_IN_PARTY", f"{o.name} zaten bir grupta")
        if party and len(party.members) >= PARTY_MAX:
            raise SimError("PARTY_FULL", "Grup dolu")
        self.world.party_invites[o.pid] = p.pid
        self.world.client_event(o, "party_invite", leader_vid=p.vid, leader_name=p.name)
        return {}

    def cmd_party_answer(self, accept: bool = True) -> dict[str, Any]:
        p = self._need_game()
        inviter_pid = self.world.party_invites.pop(p.pid, None)
        if inviter_pid is None:
            raise SimError("NO_INVITE", "Bekleyen grup daveti yok")
        leader = self.world.player_by_pid(inviter_pid)
        if not accept:
            if leader:
                self.world.client_event(leader, "party_invite_declined", name=p.name)
            return {"joined": False}
        if leader is None or not leader.in_game:
            raise SimError("NO_LEADER", "Davet eden oyuncu çevrimdışı")
        if p.pid in self.world.parties:
            raise SimError("ALREADY_IN_PARTY", "Zaten bir gruptasın")
        party = self.world.parties.get(leader.pid)
        if party is None:
            party = Party(leader=leader.pid, members=[leader.pid])
            self.world.parties[leader.pid] = party
        if len(party.members) >= PARTY_MAX:
            raise SimError("PARTY_FULL", "Grup dolu")
        party.members.append(p.pid)
        self.world.parties[p.pid] = party
        for pid in party.members:
            m = self.world.player_by_pid(pid)
            if m:
                self.world.client_event(m, "party_joined", name=p.name, members=len(party.members))
        self.world.server_event("event", "PARTY_JOIN", p, leader=leader.name, members=len(party.members))
        return {"joined": True, "members": len(party.members)}

    def cmd_party_leave(self) -> dict[str, Any]:
        p = self._need_game()
        if p.pid not in self.world.parties:
            raise SimError("NOT_IN_PARTY", "Grupta değilsin")
        self.world.party_remove(p, "LEFT")
        return {}

    def cmd_party_kick(self, vid: int) -> dict[str, Any]:
        p = self._need_game()
        party = self.world.parties.get(p.pid)
        if party is None or party.leader != p.pid:
            raise SimError("NOT_LEADER", "Sadece lider atabilir")
        o = next((pl for pl in self.world.players.values() if pl.vid == int(vid)), None)
        if o is None or o.pid not in party.members or o is p:
            raise SimError("NOT_MEMBER", "Bu oyuncu grupta değil")
        self.world.party_remove(o, "KICKED")
        return {}

    def cmd_buy_item(self, slot: int) -> dict[str, Any]:
        p = self._need_free()
        shop = self.windows.get("shop")
        if not shop:
            raise SimError("NO_SHOP", "Açık dükkan yok")
        if not 0 <= slot < len(shop["items"]):
            raise SimError("BAD_SLOT", "Geçersiz dükkan slotu")
        it = shop["items"][slot]
        if p.gold < it["price"] and "negative_gold_on_buy" not in self.world.faults:
            self.push_message("Yeterli Yang yok.")
            raise SimError("NOT_ENOUGH_GOLD", "Yetersiz yang")
        if not p.add_item(it["vnum"], 1):
            self.push_message("Envanterde yeterli yer yok.")
            raise SimError("INVENTORY_FULL", "Envanter dolu")
        p.gold -= it["price"]
        self.world.server_event("event", "SHOP_BUY", p, vnum=it["vnum"], price=it["price"])
        return {"vnum": it["vnum"], "gold": p.gold}

    def cmd_sell_item(self, slot: int, count: int | None = None) -> dict[str, Any]:
        p = self._need_free()
        if "shop" not in self.windows:
            raise SimError("NO_SHOP", "Açık dükkan yok")
        s = self._slot(p, slot)
        n = s["count"] if count is None else int(count)
        if not 0 < n <= s["count"]:
            raise SimError("BAD_COUNT", "Geçersiz adet")
        vnum = s["vnum"]
        price = max(1, ITEMS[vnum]["price"] // 5) * n
        s["count"] -= n
        if s["count"] == 0:
            p.inventory[slot] = None
        if "sell_no_gold" not in self.world.faults:
            p.gold += price
        self.world.server_event("event", "SHOP_SELL", p, vnum=vnum, count=n, price=price)
        return {"gold": p.gold, "price": price}

    # ------------------------------------------------------------ chat / QA komutları
    def cmd_send_chat(self, message: str) -> dict[str, Any]:
        p = self._need_game()
        if message.startswith("/qa"):
            self._qa_command(p, message.split()[1:])
        else:
            self.log(f"chat: {message}")
        return {}

    def _qa_command(self, p: Player, parts: list[str]) -> None:
        """Sunucudaki `ACMD(do_qa)` karşılığı: yalnızca QA hesaplarına açık hazırlık komutları."""
        if not p.name.startswith(QA_PREFIX):
            self.push_message("[QA] ERR yetkisiz")
            return
        if not parts:
            self.push_message("[QA] ERR komut yok")
            return
        cmd, a = parts[0], parts[1:]
        try:
            if cmd == "reset":
                p.reset()
                self.windows.clear()
            elif cmd == "item":
                vnum, count = int(a[0]), int(a[1]) if len(a) > 1 else 1
                if vnum not in ITEMS:
                    raise ValueError(f"item {vnum} yok")
                if not p.add_item(vnum, count):
                    raise ValueError("envanter dolu")
            elif cmd == "gold":
                p.gold = int(a[0])
            elif cmd == "level":
                p.level = int(a[0])
                p.hp = p.max_hp
            elif cmd == "hp":
                p.hp = min(p.max_hp, int(a[0]))
            elif cmd == "warp":
                p.map, p.x, p.y, p.dest = int(a[0]), float(a[1]), float(a[2]), None
            elif cmd == "clear_inventory":
                p.inventory = [None] * INVENTORY_SIZE
            else:
                raise ValueError(f"bilinmeyen komut {cmd}")
        except (ValueError, IndexError) as e:
            self.push_message(f"[QA] ERR {cmd}: {e}")
            return
        self.world.server_event("qa", "QA_COMMAND", p, command=" ".join(parts))
        self.push_message(f"[QA] OK {cmd}")


class SimError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
