"""HeadlessClient: oyun client'ı olmadan, gerçek paketlerle auth/game sunucusuna bağlanan AI oyuncu.

Simülatördeki `SimClient` ile aynı `cmd_*` sözleşmesini uygular; `LocalBridge(client.handle)` ile
senaryolar, çoklu ajan, oracle, replay, Gemini keşfi ve daemon değişmeden gerçek sunucuda çalışır.

Akış: auth sunucusu (handshake → LOGIN3 → AUTH_SUCCESS anahtarı) → kanal (handshake → LOGIN2 →
LOGIN_SUCCESS karakter listesi) → CHARACTER_SELECT → MAIN_CHARACTER/POINTS/ITEM_SET → ENTERGAME → GAME.

Oyuncu gibi davranır: hareket insan hızında adım adım MOVE paketleriyle, saldırı saldırı hızında tekrar
eden ATTACK paketleriyle gönderilir (sunucunun hız kontrolüne takılmamak için). `wait(ms)` süresince gelen
paketler işlenir, PING'lere cevap verilir.

Bu sürümde desteklenenler: giriş/seçim/çıkış, durum/envanter/varlıklar, hareket, hedef/saldırı, item
kullanma/giyme, yerden toplama, NPC'ye tıklama + görev diyaloğu, chat (/qa hazırlık komutları dahil),
yeniden doğma. Dükkan, ticaret, grup ve kanal değiştirme fork paketleri doğrulanınca eklenecek
(şimdilik NOT_SUPPORTED döner).
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any, Callable

from ..bridge.protocol import PROTOCOL_VERSION, err, ok
from ..config import HeadlessConfig
from ..sim.png import SIZE, encode_png
from .bindings import Bindings
from .crypto import make_crypto
from .net import Packet, PacketConnection, ProtocolError
from .profile import Profile, ProfileError


class HeadlessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


NOT_SUPPORTED = {
    "buy_item", "sell_item", "use_skill", "unequip_item", "drop_item", "change_channel",
    "trade_request", "trade_add_item", "trade_set_gold", "trade_accept", "trade_cancel",
    "party_invite", "party_answer", "party_leave", "party_kick",
}


def _dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


class HeadlessClient:
    CAPABILITIES = ["screenshot", "headless"]

    def __init__(self, cfg: HeadlessConfig, resolve: Callable[[str], Path] = Path,
                 profile: Profile | None = None, bindings: Bindings | None = None):
        self.cfg = cfg
        if profile is None:
            ppath = Path(resolve(cfg.profile))
            profile = Profile.load(ppath)
            bindings = bindings or Bindings.for_profile(profile, ppath)
        self.profile = profile
        self.b = bindings or Bindings(profile)
        self._t0 = time.monotonic()
        self.conn: PacketConnection | None = None
        self._reset_state()

    # ------------------------------------------------------------------ durum
    def _reset_state(self) -> None:
        self.phase: int | None = None
        self.login_key: int | None = None
        self.account: str | None = None
        self.characters: list[dict[str, Any]] = []
        self.in_game = False
        self.me: dict[str, Any] = {"vid": None, "name": None, "x": 0.0, "y": 0.0}
        self.points: dict[int, int] = {}
        self.chars: dict[int, dict[str, Any]] = {}
        self.ground: dict[int, dict[str, Any]] = {}
        self.items: dict[int, dict[str, Any]] = {}
        self.dialog: dict[str, Any] | None = None
        self.messages: list[dict[str, Any]] = []
        self.log_lines: list[dict[str, Any]] = []
        self._msg_seq = self._log_seq = 0
        self.dest: tuple[float, float] | None = None
        self._next_move = 0.0
        self.target: int | None = None
        self.attacking = False
        self._next_attack = 0.0
        self.dead = False
        self._pending: list[dict[str, Any]] = []
        self._auth_result: dict[str, Any] | None = None

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def log(self, text: str) -> None:
        self._log_seq += 1
        self.log_lines.append({"seq": self._log_seq, "t": self.now_ms(), "text": text})
        del self.log_lines[:-2000]

    def event(self, event_name: str, **data: Any) -> None:
        self._pending.append({"event": event_name, "t": self.now_ms(), "data": data})

    def message(self, text: str) -> None:
        self._msg_seq += 1
        self.messages.append({"seq": self._msg_seq, "t": self.now_ms(), "text": text})
        del self.messages[:-500]

    # ------------------------------------------------------------------ bridge sözleşmesi
    def handle(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        req_id = msg.get("id")
        cmd = msg.get("cmd", "")
        args = msg.get("args") or {}
        try:
            if cmd in NOT_SUPPORTED:
                raise HeadlessError("NOT_SUPPORTED", f"'{cmd}' headless client'ta henüz yok "
                                    "(fork paketleri doğrulanınca eklenecek)")
            fn = getattr(self, f"cmd_{cmd}", None)
            if fn is None:
                raise HeadlessError("UNKNOWN_COMMAND", f"Bilinmeyen komut: {cmd}")
            resp = ok(req_id, self.now_ms(), fn(**args))
        except HeadlessError as e:
            self.log(f"{cmd} reddedildi: {e.code} {e.message}")
            resp = err(req_id, self.now_ms(), e.code, e.message)
        except (ProtocolError, ProfileError) as e:
            self.log(f"{cmd}: protokol hatası: {e}")
            self._drop_connection()
            resp = err(req_id, self.now_ms(), "DISCONNECTED", str(e))
        except TypeError as e:
            resp = err(req_id, self.now_ms(), "BAD_ARGS", str(e))
        out, self._pending = self._pending, []
        return out + [resp]

    def close(self) -> None:
        self._drop_connection()

    def _drop_connection(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        self.in_game = False

    # ------------------------------------------------------------------ paket pompası
    def _connect(self, host: str, port: int) -> PacketConnection:
        conn = PacketConnection(self.profile, host, port, self.cfg.timeout_s, make_crypto(self.cfg.crypto), log=self.log)
        conn.connect()
        return conn

    def _pump(self, ms: float) -> None:
        """ms boyunca gelen paketleri işle; hareket/saldırı döngülerini ilerlet."""
        deadline = time.monotonic() + ms / 1000.0
        while True:
            self._tick()
            left = deadline - time.monotonic()
            if self.conn is None:
                if left > 0:
                    time.sleep(min(left, 0.05))
                    continue
                break
            step = min(max(left, 0.0), 0.05)
            for p in self.conn.poll(step):
                self._on_packet(p)
            if left <= 0:
                break

    def _pump_until(self, cond: Callable[[], bool], timeout_s: float, what: str) -> None:
        deadline = time.monotonic() + timeout_s
        while not cond():
            if time.monotonic() > deadline:
                raise HeadlessError("TIMEOUT", f"{what} beklenirken zaman aşımı")
            if self.conn is None:
                raise HeadlessError("DISCONNECTED", f"{what} beklenirken bağlantı koptu")
            self._pump(50)

    def _send(self, logical: str, trailing: bytes = b"", **kv: Any) -> None:
        if self.conn is None:
            raise HeadlessError("NOT_CONNECTED", "Sunucuya bağlı değil")
        h = self.b.cg(logical)
        self.conn.send(h, self.b.values(logical, h, **kv), trailing)

    def _on_packet(self, p: Packet) -> None:
        logical = self.b.gc_names.get(p.name)
        g = lambda key, default=None: self.b.get(logical, key, p.name, p.data, default)  # noqa: E731
        if logical == "handshake":
            # Sunucunun handshake'ini aynen geri gönder (klasik davranış)
            vals = {k: v for k, v in p.data.items()}
            h = self.b.cg("handshake")
            first = self.profile.structs[self.profile.resolve(self.profile.struct_for(h))].fields[0].name
            vals.pop(first, None)
            self.conn.send(h, vals)
        elif logical == "ping":
            self._send("ping") if self.b.supports("ping") else None
        elif logical == "phase":
            self.phase = g("phase")
            self.log(f"faz {self.phase}")
        elif logical == "auth_success":
            self._auth_result = {"key": g("key"), "result": g("result", 1)}
        elif logical == "login_failure":
            self._auth_result = {"failure": g("status", "?")}
        elif logical == "login_success":
            players = g("players", []) or []
            self.characters = []
            for i, pl in enumerate(players):
                name = pl.get("szName") or pl.get("name")
                if name:
                    self.characters.append({"index": i, "name": name, "level": pl.get("byLevel", pl.get("level")),
                                            "id": pl.get("dwID")})
        elif logical == "main_character":
            self.me.update(vid=g("vid"), name=g("name"), x=float(g("x", 0)), y=float(g("y", 0)))
        elif logical == "points":
            arr = g("array", []) or []
            self.points = {i: v for i, v in enumerate(arr)}
        elif logical == "point_change":
            if g("vid") in (None, 0, self.me["vid"]):
                self.points[g("type")] = g("value")
                if g("type") == self.b["points"]["HP"] and g("value", 1) <= 0:
                    self._on_dead(self.me["vid"])
        elif logical == "char_add":
            vid = g("vid")
            if vid == self.me["vid"]:
                return
            self.chars[vid] = {"vid": vid, "x": float(g("x", 0)), "y": float(g("y", 0)),
                               "type": self.b["char_types"].get(g("type"), "other"), "race": g("race", 0),
                               "name": self.chars.get(vid, {}).get("name", ""), "dead": False}
        elif logical == "char_info":
            vid = g("vid")
            if vid in self.chars:
                self.chars[vid]["name"] = g("name", "")
        elif logical == "char_del":
            vid = g("vid")
            c = self.chars.pop(vid, None)
            if vid == self.target and c is not None and self.attacking:
                self.attacking = False
                self.event("entity_dead", vid=vid, vnum=c.get("race"))
        elif logical == "char_move":
            vid = g("vid")
            if vid == self.me["vid"]:
                return
            if vid in self.chars:
                self.chars[vid].update(x=float(g("x", 0)), y=float(g("y", 0)))
        elif logical == "dead":
            self._on_dead(g("vid"))
        elif logical == "item_set":
            pos = g("pos") or {}
            cell = pos.get("cell") if isinstance(pos, dict) else pos
            win = pos.get("window_type", self.b["inventory_window"]) if isinstance(pos, dict) else self.b["inventory_window"]
            if win != self.b["inventory_window"]:
                return
            vnum, count = g("vnum", 0), g("count", 0)
            if vnum:
                self.items[cell] = {"vnum": vnum, "count": count}
            else:
                self.items.pop(cell, None)
        elif logical == "item_ground_add":
            vid = g("vid")
            self.ground[vid] = {"vid": vid, "vnum": g("vnum"), "x": float(g("x", 0)), "y": float(g("y", 0))}
            self.event("item_dropped", vid=vid, vnum=g("vnum"))
        elif logical == "item_ground_del":
            self.ground.pop(g("vid"), None)
        elif logical == "chat_gc":
            text = p.trailing.split(b"\0", 1)[0].decode(self.profile.encoding, errors="replace")
            ctype = g("type")
            ct = self.b["chat_types"]
            if ctype in (ct["INFO"], ct["NOTICE"], ct.get("COMMAND", -1)):
                self.message(text)
            else:
                self.log(f"chat: {text}")
        elif logical == "script":
            self._on_script(p.trailing)

    def _on_dead(self, vid: int | None) -> None:
        if vid == self.me["vid"]:
            self.dead, self.attacking, self.dest = True, False, None
            self.event("player_dead")
        elif vid in self.chars:
            self.chars[vid]["dead"] = True
            if vid == self.target:
                self.attacking = False
                self.event("entity_dead", vid=vid, vnum=self.chars[vid].get("race"))

    def _on_script(self, raw: bytes) -> None:
        text = raw.split(b"\0", 1)[0].decode(self.profile.encoding, errors="replace")
        options: list[tuple[int, str]] = []
        q = re.search(r"\[QUESTION ([^\]]*)\]", text)
        if q:
            for i, part in enumerate(q.group(1).split("|")):
                num, _, label = part.partition(";")
                options.append((int(num) if num.strip().isdigit() else i, label or part))
        clean = re.sub(r"\[[^\]]*\]", " ", text)
        clean = " ".join(clean.split())
        self.dialog = {"text": clean, "options": [o[1] for o in options] or ["Kapat"],
                       "answers": [o[0] for o in options] or [self.b["script_close_answer"]]}
        self.event("window_opened", name="dialog")

    def _tick(self) -> None:
        """Hareket ve saldırı döngüleri: gerçek client gibi zamanla paket gönderir."""
        if not self.in_game or self.conn is None or self.dead:
            return
        now = time.monotonic()
        funcs = self.b["move_funcs"]
        if self.dest and now >= self._next_move:
            interval = self.cfg.move_interval_ms / 1000.0
            dx, dy = self.dest[0] - self.me["x"], self.dest[1] - self.me["y"]
            d = math.hypot(dx, dy)
            step = self.cfg.walk_speed * interval
            rot = int((math.degrees(math.atan2(dx, -dy)) % 360) / 5) if d else 0
            if d <= step:
                self.me["x"], self.me["y"] = self.dest
                self.dest = None
                self._send("move", func=funcs["WAIT"], arg=0, rot=rot, x=int(self.me["x"]), y=int(self.me["y"]),
                           time=self.now_ms())
            else:
                self.me["x"] += dx / d * step
                self.me["y"] += dy / d * step
                self._send("move", func=funcs["MOVE"], arg=0, rot=rot, x=int(self.me["x"]), y=int(self.me["y"]),
                           time=self.now_ms())
            self._next_move = now + interval
        if self.attacking and now >= self._next_attack:
            t = self.chars.get(self.target or -1)
            if t is None or t.get("dead"):
                self.attacking = False
            elif _dist(self.me["x"], self.me["y"], t["x"], t["y"]) <= self.b["attack_range"] * 1.5:
                self._send("attack", type=0, vid=t["vid"])
            self._next_attack = now + self.b["attack_interval_ms"] / 1000.0

    # ------------------------------------------------------------------ yardımcı
    def _need_game(self) -> None:
        if not self.in_game or self.conn is None:
            raise HeadlessError("NOT_IN_GAME", "Karakter oyunda değil")

    def _need_alive(self) -> None:
        self._need_game()
        if self.dead:
            raise HeadlessError("DEAD", "Karakter ölü")

    def _map_index(self) -> int | None:
        x, y = self.me["x"], self.me["y"]
        for m in self.cfg.maps:
            if m["x"] <= x < m["x"] + m["width"] and m["y"] <= y < m["y"] + m["height"]:
                return m["index"]
        return None

    def _point(self, key: str, default: Any = None) -> Any:
        return self.points.get(self.b["points"][key], default)

    def _entity(self, vid: int) -> dict[str, Any]:
        if vid in self.chars:
            return self.chars[vid]
        if vid in self.ground:
            return {**self.ground[vid], "type": "item"}
        raise HeadlessError("NO_ENTITY", f"VID {vid} bulunamadı")

    def _in_range(self, e: dict[str, Any], r: float) -> None:
        if _dist(self.me["x"], self.me["y"], e["x"], e["y"]) > r:
            raise HeadlessError("OUT_OF_RANGE", "Hedef çok uzakta")

    # ------------------------------------------------------------------ durum komutları
    def cmd_hello(self) -> dict[str, Any]:
        return {"client": "metin2_qa_headless", "protocol": PROTOCOL_VERSION, "capabilities": self.CAPABILITIES,
                "profile": self.profile.name}

    def cmd_wait(self, ms: int) -> dict[str, Any]:
        self._pump(int(ms))
        return {}

    def cmd_get_player_state(self) -> dict[str, Any]:
        if self.conn is not None:
            self._pump(0)
        if not self.in_game:
            return {"in_game": False, "logged_in": self.account is not None}
        return {"in_game": True, "logged_in": True, "name": self.me["name"], "vid": self.me["vid"],
                "level": self._point("LEVEL"), "exp": self._point("EXP"), "hp": self._point("HP"),
                "max_hp": self._point("MAX_HP"), "sp": self._point("SP"), "max_sp": self._point("MAX_SP"),
                "gold": self._point("GOLD"), "x": round(self.me["x"]), "y": round(self.me["y"]), "map": self._map_index(),
                "channel": self.cfg.channel, "dead": self.dead, "moving": self.dest is not None,
                "attacking": self.attacking, "target_vid": self.target}

    def cmd_get_inventory(self) -> dict[str, Any]:
        self._need_game()
        size = self.b["inventory_size"]
        items = [{"slot": c, "vnum": v["vnum"], "count": v["count"], "name": str(v["vnum"])}
                 for c, v in sorted(self.items.items()) if c < size]
        wear = self.b["wear_names"]
        eq = {wear.get(c - size, f"wear{c - size}"): {"vnum": v["vnum"], "name": str(v["vnum"])}
              for c, v in self.items.items() if c >= size}
        return {"size": size, "items": items, "equipment": eq}

    def cmd_get_nearby_entities(self, radius: int = 5000, type: str | None = None,
                                vnum: int | None = None) -> list[dict[str, Any]]:
        self._need_game()
        rows = []
        for c in self.chars.values():
            if c.get("dead") and c["type"] == "monster":
                continue
            rows.append({"vid": c["vid"], "type": c["type"], "vnum": c["race"], "name": c.get("name", ""),
                         "x": round(c["x"]), "y": round(c["y"])})
        for g in self.ground.values():
            rows.append({"vid": g["vid"], "type": "item", "vnum": g["vnum"], "name": str(g["vnum"]),
                         "x": round(g["x"]), "y": round(g["y"]), "count": 1})
        out = []
        for r in rows:
            if type and r["type"] != type or vnum is not None and r["vnum"] != vnum:
                continue
            d = _dist(self.me["x"], self.me["y"], r["x"], r["y"])
            if d <= radius:
                out.append({**r, "distance": round(d)})
        out.sort(key=lambda r: (r["distance"], r["vid"]))
        return out

    def cmd_get_target(self) -> dict[str, Any] | None:
        self._need_game()
        if not self.target:
            return None
        c = self.chars.get(self.target)
        if c is None:
            return {"vid": self.target, "dead": True}
        return {"vid": c["vid"], "type": c["type"], "vnum": c["race"], "name": c.get("name"), "dead": c.get("dead")}

    def cmd_get_open_windows(self) -> list[dict[str, Any]]:
        return [{"name": "dialog", "text": self.dialog["text"], "options": self.dialog["options"]}] if self.dialog else []

    def cmd_get_quest_state(self) -> dict[str, Any]:
        return {}

    def cmd_get_party(self) -> dict[str, Any]:
        return {"in_party": False, "members": []}

    def cmd_get_system_messages(self, since: int = 0) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["seq"] > since]

    def cmd_get_client_log(self, since: int = 0) -> list[dict[str, Any]]:
        return [m for m in self.log_lines if m["seq"] > since]

    def cmd_screenshot(self) -> dict[str, Any]:
        """Gerçek ekran yok: çevrenin kuşbakışı haritası (varlıkların konumları) PNG olarak."""
        import base64

        self._need_game()
        view = 6000
        px = [bytearray((34, 60, 34) * SIZE) for _ in range(SIZE)]
        colors = {"pc": (180, 180, 255), "monster": (220, 50, 50), "npc": (60, 120, 255), "item": (255, 220, 0)}

        def dot(x: float, y: float, col: tuple[int, int, int], r: int) -> None:
            cx = int((x - self.me["x"]) / view * SIZE + SIZE / 2)
            cy = int((y - self.me["y"]) / view * SIZE + SIZE / 2)
            for yy in range(cy - r, cy + r + 1):
                for xx in range(cx - r, cx + r + 1):
                    if 0 <= xx < SIZE and 0 <= yy < SIZE:
                        px[yy][xx * 3:xx * 3 + 3] = bytes(col)

        for e in self.cmd_get_nearby_entities(radius=view):
            dot(e["x"], e["y"], colors.get(e["type"], (200, 200, 200)), 2 if e["type"] == "item" else 3)
        dot(self.me["x"], self.me["y"], (255, 255, 255), 4)
        return {"format": "png", "base64": base64.b64encode(encode_png(px)).decode()}

    # ------------------------------------------------------------------ oturum
    def cmd_login(self, account: str, password: str) -> dict[str, Any]:
        self._drop_connection()
        self._reset_state()
        self.account = None
        timeout = self.cfg.timeout_s
        # 1) auth sunucusu: anahtar al
        self.conn = self._connect(self.cfg.auth_host, self.cfg.auth_port)
        try:
            auth_phase = self.b.phase("AUTH")
            self._pump_until(lambda: auth_phase is None or self.phase == auth_phase, timeout, "auth fazı")
            self._send("auth_login", login=account, password=password)
            self._pump_until(lambda: self._auth_result is not None, timeout, "auth yanıtı")
        finally:
            self._drop_connection()
        res = self._auth_result or {}
        if "failure" in res or not res.get("key") or res.get("result") == 0:
            raise HeadlessError("LOGIN_FAILED", f"Giriş reddedildi: {res.get('failure') or res}")
        self.login_key = res["key"]
        # 2) kanal (game) sunucusu: karakter listesi
        port = self.cfg.channels.get(str(self.cfg.channel)) or next(iter(self.cfg.channels.values()))
        self.conn = self._connect(self.cfg.game_host, int(port))
        login_phase = self.b.phase("LOGIN")
        self._pump_until(lambda: login_phase is None or self.phase == login_phase, timeout, "login fazı")
        self._send("game_login", login=account, key=self.login_key)
        select_phase = self.b.phase("SELECT")
        self._pump_until(lambda: bool(self.characters) or self.phase == select_phase, timeout, "karakter listesi")
        self.account = account
        self.log(f"login {account}: {len(self.characters)} karakter")
        return {"characters": [{k: c[k] for k in ("index", "name", "level")} for c in self.characters]}

    def cmd_select_character(self, name: str | None = None, index: int | None = None) -> dict[str, Any]:
        if self.account is None or self.conn is None:
            raise HeadlessError("NOT_LOGGED_IN", "Önce login olunmalı")
        if name is not None:
            match = [c for c in self.characters if c["name"] == name]
            if not match:
                raise HeadlessError("NO_CHARACTER", f"'{name}' karakteri yok")
            index = match[0]["index"]
        index = int(index or 0)
        self._send("select", index=index)
        self._pump_until(lambda: self.me["vid"] is not None, self.cfg.timeout_s, "ana karakter")
        loading = self.b.phase("LOADING")
        self._pump_until(lambda: loading is None or self.phase == loading, self.cfg.timeout_s, "yükleme fazı")
        self._send("enter_game")
        game = self.b.phase("GAME")
        self._pump_until(lambda: game is None or self.phase == game, self.cfg.timeout_s, "oyun fazı")
        self.in_game = True
        self._pump(300)  # etraftaki karakter/item paketleri gelsin
        self.event("map_loaded", map=self._map_index())
        return {"name": self.me["name"], "map": self._map_index()}

    def cmd_logout(self) -> dict[str, Any]:
        self._need_game()
        self._drop_connection()
        self.account = None
        return {}

    # ------------------------------------------------------------------ aksiyonlar
    def cmd_move_to(self, x: float, y: float) -> dict[str, Any]:
        self._need_alive()
        self.dest = (float(x), float(y))
        self.attacking = False
        self._next_move = 0.0
        return {}

    def cmd_target(self, vid: int) -> dict[str, Any]:
        self._need_alive()
        e = self._entity(int(vid))
        if e.get("dead"):
            raise HeadlessError("TARGET_DEAD", "Hedef ölü")
        self._send("target", vid=int(vid))
        self.target = int(vid)
        return {}

    def cmd_attack(self) -> dict[str, Any]:
        self._need_alive()
        t = self.chars.get(self.target or -1)
        if t is None or t["type"] != "monster" or t.get("dead"):
            raise HeadlessError("NO_TARGET", "Saldırılacak hedef yok")
        self._in_range(t, self.b["attack_range"])
        self.dest = None
        self.attacking = True
        self._next_attack = 0.0
        self._tick()
        return {}

    def cmd_stop_attack(self) -> dict[str, Any]:
        self.attacking = False
        return {}

    def _item_pos(self, slot: int) -> dict[str, Any]:
        if slot not in self.items:
            raise HeadlessError("EMPTY_SLOT", f"Slot {slot} boş")
        return {"window_type": self.b["inventory_window"], "cell": int(slot)}

    def cmd_use_item(self, slot: int) -> dict[str, Any]:
        self._need_alive()
        self._send("item_use", pos=self._item_pos(int(slot)))
        self._pump(150)
        return {}

    def cmd_equip_item(self, slot: int) -> dict[str, Any]:
        return self.cmd_use_item(slot)  # Metin2'de giyilebilir item kullanılınca giyilir

    def cmd_pickup(self, vid: int) -> dict[str, Any]:
        self._need_alive()
        g = self.ground.get(int(vid))
        if g is None:
            raise HeadlessError("NO_ENTITY", f"Yerde VID {vid} yok")
        self._in_range(g, self.b["attack_range"])
        self._send("item_pickup", vid=int(vid))
        self._pump(150)
        return {"vnum": g["vnum"]}

    def cmd_talk_to_npc(self, vid: int) -> dict[str, Any]:
        self._need_alive()
        e = self._entity(int(vid))
        self._in_range(e, self.b["interact_range"])
        self._send("on_click", vid=int(vid))
        self._pump(300)
        return {"window": "dialog" if self.dialog else None}

    def cmd_select_dialog(self, index: int) -> dict[str, Any]:
        self._need_alive()
        d = self.dialog
        if not d:
            raise HeadlessError("NO_DIALOG", "Açık dialog yok")
        if not 0 <= int(index) < len(d["options"]):
            raise HeadlessError("BAD_OPTION", "Geçersiz seçenek")
        self.dialog = None
        self._send("script_answer", answer=d["answers"][int(index)])
        self._pump(200)
        return {"selected": d["options"][int(index)]}

    def cmd_close_window(self, name: str) -> dict[str, Any]:
        if name == "dialog" and self.dialog:
            self.dialog = None
            self._send("script_answer", answer=self.b["script_close_answer"])
        return {}

    def cmd_send_chat(self, message: str) -> dict[str, Any]:
        self._need_game()
        raw = message.encode(self.profile.encoding, errors="replace") + b"\0"
        self._send("chat", trailing=raw, type=self.b["chat_types"]["TALKING"])
        self._pump(100)
        return {}

    def cmd_respawn(self, here: bool = False) -> dict[str, Any]:
        self._need_game()
        if not self.dead:
            raise HeadlessError("NOT_DEAD", "Karakter ölü değil")
        self.cmd_send_chat("/restart_here" if here else "/restart_town")
        self._pump(500)
        self.dead = False
        return {}
