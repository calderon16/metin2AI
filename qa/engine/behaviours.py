"""Yüksek seviyeli, deterministik oyuncu davranışları.

LLM ("10 köpek kes") ile istemci komutları (move_to, target, attack ...) arasındaki katman.
Her behaviour yalnızca bridge aksiyonlarını kullanır — yani gerçek oyuncunun gönderdiği
paketlerle aynı yoldan geçer. Hedefe ulaşılamazsa BehaviourError fırlatır; sonucun
"doğru" olup olmadığına oracle (assertion'lar) karar verir.
"""

from __future__ import annotations

import math
from typing import Any

from ..bridge.protocol import ActionError
from .executor import BehaviourError, GameContext, behaviour

APPROACH_RANGE = 200
PICKUP_RANGE = 150


def _dist(a: dict[str, Any], x: float, y: float) -> float:
    return math.hypot(a["x"] - x, a["y"] - y)


def _alive_state(ctx: GameContext) -> dict[str, Any]:
    s = ctx.state()
    if not s.get("in_game"):
        raise BehaviourError("NOT_IN_GAME", "Karakter oyunda değil")
    if s.get("dead"):
        raise BehaviourError("PLAYER_DEAD", "Karakter öldü", hp=s.get("hp"))
    return s


def _find(ctx: GameContext, type: str | None = None, vnum: int | None = None, vid: int | None = None,
          radius: int = 15000) -> dict[str, Any] | None:
    filters: dict[str, Any] = {"radius": radius}
    if type:
        filters["type"] = type
    if vnum is not None:
        filters["vnum"] = vnum
    rows = ctx.entities(**filters)
    if vid is not None:
        rows = [r for r in rows if r["vid"] == vid]
    return rows[0] if rows else None


# ------------------------------------------------------------------ hareket

@behaviour("walk_to")
def walk_to(ctx: GameContext, x: int, y: int, tolerance: int = 150, timeout_ms: int = 60000) -> dict[str, Any]:
    """(x, y) noktasına yürü. Hedef tolerance içinde rastgele kaydırılır (farklı rotalar)."""
    tx = ctx.rng.jitter(x, tolerance / 3)
    ty = ctx.rng.jitter(y, tolerance / 3)
    ctx.act("move_to", x=round(tx), y=round(ty))
    start, retries = ctx.now(), 0
    while True:
        ctx.wait(ctx.rng.movement_delay())
        s = _alive_state(ctx)
        d = _dist(s, x, y)
        if d <= tolerance:
            return {"x": s["x"], "y": s["y"], "distance": round(d)}
        if ctx.now() - start > timeout_ms:
            raise BehaviourError("TIMEOUT", f"({x},{y}) noktasına ulaşılamadı", position=[s["x"], s["y"]])
        if not s.get("moving"):
            retries += 1
            if retries > 3:
                raise BehaviourError("STUCK", "Karakter hedefe ilerlemiyor", position=[s["x"], s["y"]])
            ctx.act("move_to", x=round(tx), y=round(ty))


@behaviour("move_to_entity")
def move_to_entity(ctx: GameContext, vnum: int | None = None, vid: int | None = None, type: str | None = None,
                   range: int = APPROACH_RANGE, radius: int = 15000, timeout_ms: int = 60000) -> dict[str, Any]:
    """En yakın varlığa (vnum/vid/type ile) yaklaş."""
    if vnum is None and vid is None:
        raise BehaviourError("BAD_ARGS", "vnum veya vid gerekli")
    start = ctx.now()
    while True:
        e = _find(ctx, type, vnum, vid, radius)
        if e is None:
            raise BehaviourError("NOT_FOUND", f"Varlık bulunamadı (vnum={vnum}, vid={vid}, type={type})")
        if e["distance"] <= range:
            return e
        if ctx.now() - start > timeout_ms:
            raise BehaviourError("TIMEOUT", f"{e['name']} yanına ulaşılamadı")
        s = _alive_state(ctx)
        # Varlığa, range'in biraz içinde kalacak şekilde yaklaş
        d = max(e["distance"], 1)
        k = (range * 0.6) / d
        walk_to(ctx, round(e["x"] + (s["x"] - e["x"]) * k), round(e["y"] + (s["y"] - e["y"]) * k),
                tolerance=max(50, range // 3), timeout_ms=timeout_ms)


# ------------------------------------------------------------------ savaş

def _maybe_heal(ctx: GameContext, s: dict[str, Any], potion_vnum: int, below_pct: int) -> None:
    if s["max_hp"] and s["hp"] * 100 / s["max_hp"] < below_pct:
        slot = ctx.find_slot(potion_vnum)
        if slot is not None:
            ctx.act("use_item", slot=slot)


def _kill_one(ctx: GameContext, vnum: int, deadline: int, auto_potion: bool, potion_vnum: int,
              potion_below_pct: int, search_radius: int, use_skill: int | None) -> dict[str, Any]:
    while True:
        if ctx.now() > deadline:
            raise BehaviourError("TIMEOUT", f"vnum {vnum} öldürülemedi (süre doldu)")
        mob = _find(ctx, "monster", vnum, radius=search_radius)
        if mob is None:
            ctx.wait(1000)  # respawn bekle
            continue
        try:
            move_to_entity(ctx, vid=mob["vid"], type="monster", range=APPROACH_RANGE, radius=search_radius)
            ctx.act("target", vid=mob["vid"])
            ctx.react()
            ctx.act("attack")
        except BehaviourError as e:
            if e.code in {"NOT_FOUND", "TIMEOUT", "STUCK"}:
                ctx.wait(300)
                continue  # hedef öldü/kayboldu, başka mob seç
            raise
        except ActionError as e:
            if e.code in {"OUT_OF_RANGE", "TARGET_DEAD", "NO_TARGET", "NO_ENTITY"}:
                ctx.wait(300)
                continue
            raise
        mark = len(ctx.events)
        skill_used = False
        while True:
            ctx.wait(ctx.rng.delay(200, 400))
            if any(e["data"].get("vid") == mob["vid"] for e in ctx.events_since(mark, "entity_dead")):
                return mob
            s = _alive_state(ctx)
            if auto_potion:
                _maybe_heal(ctx, s, potion_vnum, potion_below_pct)
            if use_skill is not None and not skill_used and s.get("sp", 0) >= 20:
                skill_used = True
                try:
                    ctx.act("use_skill", slot=use_skill)
                except ActionError:
                    pass
            if ctx.now() > deadline:
                raise BehaviourError("TIMEOUT", f"{mob['name']} ile savaş bitmedi")
            if not s.get("attacking"):
                target = ctx.query("get_target")
                if not target or target.get("dead"):
                    # Başkası öldürdü ya da hedef düştü
                    break
                try:
                    move_to_entity(ctx, vid=mob["vid"], type="monster", range=APPROACH_RANGE)
                    ctx.act("attack")
                except (BehaviourError, ActionError):
                    break


@behaviour("kill_monster")
def kill_monster(ctx: GameContext, vnum: int, count: int = 1, timeout_ms: int = 180000,
                 auto_potion: bool = True, potion_vnum: int = 27001, potion_below_pct: int = 35,
                 pickup: bool = False, search_radius: int = 15000, use_skill: int | None = None) -> dict[str, Any]:
    """vnum'lu moblardan `count` tane öldür (bul → yaklaş → hedef al → saldır → gerekirse iksir)."""
    deadline = ctx.now() + timeout_ms
    killed = []
    for _ in range(count):
        mob = _kill_one(ctx, vnum, deadline, auto_potion, potion_vnum, potion_below_pct, search_radius, use_skill)
        killed.append(mob["vid"])
        ctx.react()
        if pickup:
            pickup_items(ctx, radius=600, all=True, required=False)
    return {"kills": len(killed), "vids": killed}


@behaviour("kill_until_drop")
def kill_until_drop(ctx: GameContext, vnum: int, item_vnum: int, max_kills: int = 20,
                    timeout_ms: int = 600000) -> dict[str, Any]:
    """item_vnum envantere girene kadar mob kes ve drop topla."""
    before = ctx.item_count(item_vnum)
    deadline = ctx.now() + timeout_ms
    for i in range(1, max_kills + 1):
        _kill_one(ctx, vnum, deadline, True, 27001, 35, 15000, None)
        ctx.react()
        pickup_items(ctx, vnum=item_vnum, radius=600, all=True, required=False)
        if ctx.item_count(item_vnum) > before:
            return {"kills": i, "obtained": ctx.item_count(item_vnum) - before}
    raise BehaviourError("DROP_NOT_OBTAINED", f"{max_kills} kesimde item {item_vnum} elde edilemedi",
                         kills=max_kills)


@behaviour("use_skill")
def use_skill(ctx: GameContext, slot: int) -> dict[str, Any]:
    """Seçili hedefe skill kullan."""
    r = ctx.act("use_skill", slot=slot)
    ctx.react()
    return r


@behaviour("respawn")
def respawn(ctx: GameContext, here: bool = False) -> dict[str, Any]:
    """Ölümden sonra yeniden doğ (şehirde veya olduğun yerde)."""
    ctx.act("respawn", here=here)
    ctx.react()
    return ctx.state()


# ------------------------------------------------------------------ item

@behaviour("pickup")
def pickup_items(ctx: GameContext, vnum: int | None = None, vid: int | None = None, radius: int = 1000,
                 all: bool = False, required: bool = True) -> dict[str, Any]:
    """Yerdeki item(ler)i topla."""
    picked = []
    while True:
        rows = [r for r in ctx.entities(type="item", radius=radius)
                if (vnum is None or r["vnum"] == vnum) and (vid is None or r["vid"] == vid)]
        if not rows:
            break
        it = rows[0]
        move_to_entity(ctx, vid=it["vid"], type="item", range=PICKUP_RANGE, radius=radius)
        ctx.act("pickup", vid=it["vid"])
        picked.append({"vid": it["vid"], "vnum": it["vnum"], "count": it.get("count", 1)})
        ctx.wait(ctx.rng.movement_delay())
        if not all or vid is not None:
            break
    if required and not picked:
        raise BehaviourError("NO_ITEM", f"Yerde item bulunamadı (vnum={vnum}, vid={vid})")
    return {"picked": picked}


def _resolve_slot(ctx: GameContext, vnum: int | None, slot: int | None) -> int:
    if slot is not None:
        return slot
    if vnum is None:
        raise BehaviourError("BAD_ARGS", "vnum veya slot gerekli")
    s = ctx.find_slot(vnum)
    if s is None:
        raise BehaviourError("ITEM_NOT_IN_INVENTORY", f"Envanterde {vnum} yok")
    return s


@behaviour("use_item")
def use_item(ctx: GameContext, vnum: int | None = None, slot: int | None = None, times: int = 1) -> dict[str, Any]:
    """Envanterdeki item'i kullan (iksir vb.)."""
    last = None
    for _ in range(times):
        last = ctx.act("use_item", slot=_resolve_slot(ctx, vnum, slot))
        ctx.react()
    return {"result": last}


@behaviour("equip_item")
def equip_item(ctx: GameContext, vnum: int | None = None, slot: int | None = None) -> dict[str, Any]:
    """Item'i giy."""
    r = ctx.act("equip_item", slot=_resolve_slot(ctx, vnum, slot))
    ctx.react()
    return r


@behaviour("unequip_item")
def unequip_item(ctx: GameContext, wear_slot: str) -> dict[str, Any]:
    """Ekipmanı çıkar (weapon, body ...)."""
    r = ctx.act("unequip_item", wear_slot=wear_slot)
    ctx.react()
    return r


@behaviour("drop_item")
def drop_item(ctx: GameContext, vnum: int | None = None, slot: int | None = None,
              count: int | None = None) -> dict[str, Any]:
    """Item'i yere at."""
    args: dict[str, Any] = {"slot": _resolve_slot(ctx, vnum, slot)}
    if count is not None:
        args["count"] = count
    r = ctx.act("drop_item", **args)
    ctx.react()
    return r


# ------------------------------------------------------------------ NPC / UI

@behaviour("talk_npc")
def talk_npc(ctx: GameContext, vnum: int | None = None, vid: int | None = None) -> dict[str, Any]:
    """NPC'ye yürü ve tıkla; açılan pencereyi döndür."""
    e = move_to_entity(ctx, vnum=vnum, vid=vid, type="npc", range=APPROACH_RANGE)
    ctx.act("talk_to_npc", vid=e["vid"])
    ctx.react()
    return {"npc": e["vid"], "windows": sorted(ctx.windows())}


@behaviour("select_dialog")
def select_dialog(ctx: GameContext, index: int | None = None, text: str | None = None) -> dict[str, Any]:
    """Açık dialogdan seçenek seç (index veya metin parçası ile)."""
    d = ctx.windows().get("dialog")
    if not d:
        raise BehaviourError("NO_DIALOG", "Açık dialog penceresi yok")
    if text is not None:
        matches = [i for i, o in enumerate(d["options"]) if text.lower() in o.lower()]
        if not matches:
            raise BehaviourError("NO_OPTION", f"'{text}' seçeneği yok", options=d["options"])
        index = matches[0]
    if index is None:
        raise BehaviourError("BAD_ARGS", "index veya text gerekli")
    r = ctx.act("select_dialog", index=index)
    ctx.react()
    return r


def _ensure_shop(ctx: GameContext, npc_vnum: int | None) -> dict[str, Any]:
    shop = ctx.windows().get("shop")
    if shop is None and npc_vnum is not None:
        talk_npc(ctx, vnum=npc_vnum)
        shop = ctx.windows().get("shop")
    if shop is None:
        raise BehaviourError("NO_SHOP", "Açık dükkan yok (npc_vnum ver veya önce talk_npc)")
    return shop


@behaviour("buy_item")
def buy_item(ctx: GameContext, vnum: int, count: int = 1, npc_vnum: int | None = None) -> dict[str, Any]:
    """NPC dükkanından item satın al."""
    shop = _ensure_shop(ctx, npc_vnum)
    slot = next((i["slot"] for i in shop["items"] if i["vnum"] == vnum), None)
    if slot is None:
        raise BehaviourError("NOT_IN_SHOP", f"Dükkanda {vnum} yok", items=[i["vnum"] for i in shop["items"]])
    for _ in range(count):
        ctx.act("buy_item", slot=slot)
        ctx.react()
    return {"bought": count}


@behaviour("sell_item")
def sell_item(ctx: GameContext, vnum: int, count: int | None = None, npc_vnum: int | None = None) -> dict[str, Any]:
    """Envanterdeki item'i açık NPC dükkanına sat."""
    _ensure_shop(ctx, npc_vnum)
    args: dict[str, Any] = {"slot": _resolve_slot(ctx, vnum, None)}
    if count is not None:
        args["count"] = count
    r = ctx.act("sell_item", **args)
    ctx.react()
    return r


@behaviour("close_window")
def close_window(ctx: GameContext, name: str) -> dict[str, Any]:
    """Pencereyi kapat (shop, dialog ...)."""
    ctx.act("close_window", name=name)
    return {}


# ------------------------------------------------------------------ genel

@behaviour("wait")
def wait(ctx: GameContext, ms: int) -> dict[str, Any]:
    """Oyun zamanında bekle."""
    ctx.wait(ms)
    return {}


@behaviour("wait_for_event")
def wait_for_event(ctx: GameContext, event: str, timeout_ms: int = 10000,
                   match: dict[str, Any] | None = None) -> dict[str, Any]:
    """İstemci olayı gelene kadar bekle (ör. entity_dead, level_up)."""
    mark, start = len(ctx.events), ctx.now()
    while ctx.now() - start <= timeout_ms:
        for e in ctx.events_since(mark, event):
            if all(e["data"].get(k) == v for k, v in (match or {}).items()):
                return e["data"]
        ctx.wait(200)
    raise BehaviourError("TIMEOUT", f"'{event}' olayı gelmedi")


@behaviour("chat")
def chat(ctx: GameContext, message: str) -> dict[str, Any]:
    """Chat mesajı gönder (QA komutları setup bölümüne aittir, burada yasak)."""
    if message.lstrip().startswith("/qa"):
        raise BehaviourError("FORBIDDEN", "/qa komutları sadece setup'ta kullanılabilir")
    ctx.act("send_chat", message=message)
    return {}


@behaviour("change_channel")
def change_channel(ctx: GameContext, channel: int) -> dict[str, Any]:
    """Kanal değiştir."""
    r = ctx.act("change_channel", channel=channel)
    ctx.react()
    return r


@behaviour("reconnect")
def reconnect(ctx: GameContext) -> dict[str, Any]:
    """Logout → login → karakter seç (kalıcılık/DB testi için)."""
    ctx.act("logout")
    ctx.wait(ctx.rng.delay(500, 1500))
    login(ctx)
    return ctx.state()


@behaviour("screenshot")
def screenshot(ctx: GameContext, label: str = "shot") -> dict[str, Any]:
    """Görsel doğrulama için ekran görüntüsü al."""
    return {"path": ctx.screenshot(label)}


# ------------------------------------------------------------------ çoklu ajan: ticaret

def _player_vid(ctx: GameContext, agent: str | None, vid: int | None) -> int:
    if agent is not None:
        return ctx.peer_vid(agent)
    if vid is None:
        raise BehaviourError("BAD_ARGS", "agent veya vid gerekli")
    return vid


def _wait_window(ctx: GameContext, name: str, timeout_ms: int) -> dict[str, Any]:
    start = ctx.now()
    while True:
        w = ctx.windows().get(name)
        if w is not None:
            return w
        if ctx.now() - start > timeout_ms:
            raise BehaviourError("TIMEOUT", f"'{name}' penceresi açılmadı")
        ctx.wait(200)


@behaviour("trade_with")
def trade_with(ctx: GameContext, agent: str | None = None, vid: int | None = None,
               timeout_ms: int = 10000) -> dict[str, Any]:
    """Oyuncuya (başka bir ajan veya vid) yaklaş ve ticaret başlat."""
    target = _player_vid(ctx, agent, vid)
    move_to_entity(ctx, vid=target, type="pc", range=500)
    ctx.act("trade_request", vid=target)
    ctx.react()
    return _wait_window(ctx, "trade", timeout_ms)


def _trade_window(ctx: GameContext) -> dict[str, Any]:
    w = ctx.windows().get("trade")
    if w is None:
        raise BehaviourError("NO_TRADE", "Açık ticaret penceresi yok")
    return w


@behaviour("trade_add_item")
def trade_add_item(ctx: GameContext, vnum: int | None = None, slot: int | None = None) -> dict[str, Any]:
    """Envanterdeki item'i ticaret penceresine koy (yığının tamamı)."""
    _trade_window(ctx)
    ctx.act("trade_add_item", slot=_resolve_slot(ctx, vnum, slot))
    ctx.react()
    return _trade_window(ctx)


@behaviour("trade_set_gold")
def trade_set_gold(ctx: GameContext, amount: int) -> dict[str, Any]:
    """Ticarete yang koy."""
    _trade_window(ctx)
    ctx.act("trade_set_gold", amount=amount)
    ctx.react()
    return _trade_window(ctx)


@behaviour("trade_accept")
def trade_accept(ctx: GameContext) -> dict[str, Any]:
    """Ticareti onayla. İki taraf da onaylayınca takas gerçekleşir."""
    _trade_window(ctx)
    r = ctx.act("trade_accept")
    ctx.react()
    r = r if isinstance(r, dict) else {}
    return {"completed": r.get("completed"), "cancelled": r.get("cancelled"), "trade_open": "trade" in ctx.windows()}


@behaviour("trade_cancel")
def trade_cancel(ctx: GameContext) -> dict[str, Any]:
    """Ticareti iptal et."""
    ctx.act("trade_cancel")
    ctx.react()
    return {}


# ------------------------------------------------------------------ çoklu ajan: grup

@behaviour("party_invite")
def party_invite(ctx: GameContext, agent: str | None = None, vid: int | None = None) -> dict[str, Any]:
    """Oyuncuyu gruba davet et."""
    ctx.act("party_invite", vid=_player_vid(ctx, agent, vid))
    ctx.react()
    return {}


@behaviour("party_accept")
def party_accept(ctx: GameContext, timeout_ms: int = 10000) -> dict[str, Any]:
    """Gelen grup davetini bekle ve kabul et."""
    inv = _wait_window(ctx, "party_invite", timeout_ms)
    ctx.react()
    r = ctx.act("party_answer", accept=True)
    return {"leader": inv.get("leader_name"), **(r or {})}


@behaviour("party_decline")
def party_decline(ctx: GameContext, timeout_ms: int = 10000) -> dict[str, Any]:
    """Gelen grup davetini reddet."""
    _wait_window(ctx, "party_invite", timeout_ms)
    ctx.react()
    return ctx.act("party_answer", accept=False)


@behaviour("party_leave")
def party_leave(ctx: GameContext) -> dict[str, Any]:
    """Gruptan ayrıl."""
    ctx.act("party_leave")
    ctx.react()
    return ctx.query("get_party")


@behaviour("party_kick")
def party_kick(ctx: GameContext, agent: str | None = None, vid: int | None = None) -> dict[str, Any]:
    """Üyeyi gruptan at (lider)."""
    ctx.act("party_kick", vid=_player_vid(ctx, agent, vid))
    ctx.react()
    return ctx.query("get_party")


def login(ctx: GameContext) -> dict[str, Any]:
    if not ctx.account or ctx.password is None:
        raise BehaviourError("NO_ACCOUNT", "Hesap bilgisi yok")
    chars = ctx.act("login", account=ctx.account, password=ctx.password)["characters"]
    name = ctx.character or ctx.account
    if not any(c["name"] == name for c in chars):
        raise BehaviourError("NO_CHARACTER", f"'{name}' karakteri yok", characters=[c["name"] for c in chars])
    ctx.act("select_character", name=name)
    start = ctx.now()
    while not ctx.state().get("in_game"):
        if ctx.now() - start > 30000:
            raise BehaviourError("TIMEOUT", "Harita yüklenmedi")
        ctx.wait(500)
    return ctx.state()
