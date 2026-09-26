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
from .crypto import ImprovedKeyAgreement, KeyAgreementError, make_crypto
from .net import Packet, PacketConnection, ProtocolError
from .profile import Profile, ProfileError


class HeadlessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


NOT_SUPPORTED = {
    "change_channel", "party_invite", "party_answer", "party_leave", "party_kick",
}


def _dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


class HeadlessClient:
    CAPABILITIES = ["screenshot", "headless"]

    def __init__(self, cfg: HeadlessConfig, resolve: Callable[[str], Path] = Path,
                 profile: Profile | None = None, bindings: Bindings | None = None):
        self.cfg = cfg
        self._resolve = resolve
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
        self.quest_letters: dict[int, str] = {}         # sol taraftaki görev mektupları: idx -> başlık
        self.quests: dict[str, dict[str, Any]] = {}     # Metin2Re görev motoru (MRQ_*): kimlik -> durum
        self.quests_available: dict[str, dict[str, Any]] = {}
        self.quests_done: set[str] = set()
        self.trade: dict[str, Any] | None = None
        self.shop: dict[str, Any] | None = None          # açık NPC dükkânı {npc_vid, items}
        self._shop_result: str | None = None             # son dükkân hata yanıtı (GC_NOT_ENOUGH_MONEY...)
        self.refine_window: dict[str, Any] | None = None  # sunucunun açtığı yükseltme penceresi
        self.skills: dict[int, int] = {}                 # beceri vnum -> seviye (GC_SKILL_LEVEL_NEW)
        self._last_click: int | None = None
        self.whispers: list[dict[str, Any]] = []
        self._wsp_seq = 0
        self.messages: list[dict[str, Any]] = []
        self.log_lines: list[dict[str, Any]] = []
        self._msg_seq = self._log_seq = 0
        self.dest: tuple[float, float] | None = None
        self.route: list[tuple[float, float]] = []      # yol bulmanın kalan ara noktaları
        self._next_move = 0.0
        self.target: int | None = None
        self.attacking = False
        self._next_attack = 0.0
        self.dead = False
        self._pending: list[dict[str, Any]] = []
        self._auth_result: dict[str, Any] | None = None
        self._char_index: int | None = None
        self._pending_warp: tuple[int, int, int] | None = None   # (port, x, y)

    def game_time_ms(self) -> int:
        """Sunucunun el sıkışmada verdiği saate göre istemci saati (CG_MOVE dwTime). Gerçek istemci gibi sunucu
        saatine yakın kalır; aksi halde sunucu 'SPEEDHACK: slow timer' kaydı düşer."""
        base = getattr(self, "_server_clock", None)
        if base is None:
            return self.now_ms()
        return (base[0] + int((time.monotonic() - base[1]) * 1000)) & 0xFFFFFFFF

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
        self._leave_cleanly()
        self._drop_connection()

    def _leave_cleanly(self) -> None:
        """Oyundan çıkmadan önce açık görev penceresini kapat. Sunucu, bağlantı kopsa da karakterin görev
        durumunu (cevap bekleyen seçim dahil) tutar; kapatılmazsa sonraki oturumda o görev menüde görünmez."""
        if self.conn is None or not self.in_game:
            return
        try:
            if self.dialog is not None:
                self._close_dialog()
            if self.refine_window is not None:
                self._cancel_refine()
            if self.shop is not None:
                self._close_shop()
        except (ProtocolError, HeadlessError):
            pass

    def _drop_connection(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        self.in_game = False

    # ------------------------------------------------------------------ paket pompası
    def _connect(self, host: str, port: int) -> PacketConnection:
        conn = PacketConnection(self.profile, host, port, self.cfg.timeout_s, make_crypto(self.cfg.crypto), log=self.log)
        conn.flag_sized = self.b.flag_sized()
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
                if self._pending_warp is not None:
                    break      # warp'tan sonraki paketler eski çekirdekten; yeni bağlantıda yeniden gelir
            if self._pending_warp is not None:
                self._do_warp()
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

    def _pump_for(self, cond: Callable[[], bool], timeout_s: float) -> bool:
        """cond doğru olana ya da süre dolana kadar paket işle (hata atmaz)."""
        deadline = time.monotonic() + timeout_s
        while not cond() and time.monotonic() < deadline and self.conn is not None:
            self._pump(50)
        return cond()

    def _msgs_since(self, seq: int) -> list[str]:
        return [m["text"] for m in self.messages if m["seq"] > seq]

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
            # Sunucu, yansıtılan dwTime'ı istemci saati kabul eder; hareket paketleri aynı saatle gitmeli
            t = vals.get("dwTime")
            if isinstance(t, int):
                self._server_clock = (t, time.monotonic())
        elif logical == "warp":
            self._pending_warp = (int(g("port", 0)), int(g("x", 0)), int(g("y", 0)))
            self.log(f"warp → port {self._pending_warp[0]} ({self._pending_warp[1]}, {self._pending_warp[2]})")
        elif logical == "key_agreement":
            self._on_key_agreement(g)
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
            if ctype == ct.get("COMMAND", -1) and text.startswith("MRQ_"):
                self._on_mrq(text)
            if ctype in (ct["INFO"], ct["NOTICE"], ct.get("COMMAND", -1)):
                self.message(text)
            else:
                self.log(f"chat: {text}")
        elif logical == "script":
            self._on_script(p.trailing)
        elif logical == "exchange":
            self._on_exchange(g)
        elif logical == "whisper":
            self._on_whisper(g, p.trailing)
        elif logical == "shop":
            self._on_shop(g, p.trailing)
        elif logical == "refine_info":
            t = g("table") or {}
            mats = [{"vnum": m.get("vnum"), "count": m.get("count")}
                    for m in (t.get("materials") or [])[:int(t.get("material_count") or 0)]]
            self.refine_window = {"slot": g("pos"), "type": g("type", 0), "src_vnum": t.get("src_vnum"),
                                  "result_vnum": t.get("result_vnum"), "cost": t.get("cost"),
                                  "prob": t.get("prob"), "materials": mats}
            self.event("window_opened", name="refine")
        elif logical == "skill_levels":
            self.skills = {i: int(s.get("bLevel", 0)) for i, s in enumerate(g("skills", []) or [])
                           if isinstance(s, dict) and s.get("bLevel")}

    def _do_warp(self) -> None:
        """Yeni çekirdeğe bağlan ve aynı karakterle doğrudan oyuna gir (gerçek istemcinin DirectEnter akışı)."""
        port, x, y = self._pending_warp or (0, 0, 0)
        self._pending_warp = None
        if self.account is None or self.login_key is None or self._char_index is None:
            raise HeadlessError("WARP_FAILED", "Warp için oturum bilgisi yok")
        self._drop_connection()
        self.phase, self.characters = None, []
        self.me.update(vid=None, x=float(x), y=float(y))
        self.chars.clear()
        self.ground.clear()
        self.dialog, self.dest, self.target, self.attacking = None, None, None, False
        self.route = []
        self.conn = self._connect(self.cfg.game_host, port)
        self._enter_game_with_key(self._char_index)
        self.log(f"warp tamam: {self.me['name']} → port {port}")
        self.event("map_loaded", map=self._map_index())

    def _enter_game_with_key(self, index: int) -> None:
        timeout = self.cfg.timeout_s
        login_phase = self.b.phase("LOGIN")
        self._pump_until(lambda: login_phase is None or self.phase == login_phase, timeout, "login fazı")
        self._send("game_login", login=self.account, key=self.login_key)
        select_phase = self.b.phase("SELECT")
        self._pump_until(lambda: bool(self.characters) or self.phase == select_phase, timeout, "karakter listesi")
        self._select_and_enter(index)

    def _select_and_enter(self, index: int) -> None:
        self._char_index = index
        self.in_game = False
        self._send("select", index=index)
        # Karakter başka çekirdeğin haritasındaysa sunucu hemen GC_WARP gönderir; iç içe _do_warp oyuna
        # girişi tamamlar (in_game), bu durumda buradaki bekleme de biter.
        self._pump_until(lambda: self.in_game or self.me["vid"] is not None, self.cfg.timeout_s, "ana karakter")
        loading = self.b.phase("LOADING")
        self._pump_until(lambda: self.in_game or loading is None or self.phase == loading, self.cfg.timeout_s,
                         "yükleme fazı")
        if self.in_game:
            return
        if self.cfg.client_version and self.b.supports("client_version"):
            self._send("client_version", filename="metin2_qa_headless", timestamp=str(self.cfg.client_version))
        self._send("enter_game")
        game = self.b.phase("GAME")
        self._pump_until(lambda: game is None or self.phase == game, self.cfg.timeout_s, "oyun fazı")
        self.in_game = True
        self._pump(300)  # etraftaki karakter/item paketleri gelsin

    def _on_key_agreement(self, g: Callable[..., Any]) -> None:
        """_IMPROVED_PACKET_ENCRYPTION_: sunucunun DH2 açık anahtarlarına kendi anahtarlarımızla (düz) cevap ver;
        HEADER_GC_KEY_AGREEMENT_COMPLETED'dan sonra iki yön de şifreli."""
        if self.cfg.crypto != "improved":
            raise ProtocolError("Sunucu geliştirilmiş paket şifrelemesi istiyor (KEY_AGREEMENT); "
                                "[headless] crypto = \"improved\" ayarlayın")
        agreed, length = int(g("agreed", 0)), int(g("length", 0))
        peer = bytes(g("data", []) or [])[:length]
        ka = ImprovedKeyAgreement()
        try:
            crypto = ka.agree(agreed, peer)
        except KeyAgreementError as e:
            raise ProtocolError(f"Anahtar anlaşması başarısız: {e}") from e
        h = self.b.cg("key_agreement")
        self.conn.send(h, self.b.values("key_agreement", h, agreed=agreed, length=len(ka.data), data=ka.data))
        self.conn.switch_crypto_after(self.b.gc("key_agreement_completed"), crypto)
        self.log(f"anahtar anlaşması: gönderim {crypto.algorithms[1]}, alım {crypto.algorithms[0]}")

    def _on_dead(self, vid: int | None) -> None:
        if vid == self.me["vid"]:
            self.dead, self.attacking, self.dest, self.route = True, False, None, []
            self.event("player_dead")
        elif vid in self.chars:
            self.chars[vid]["dead"] = True
            if vid == self.target:
                self.attacking = False
                self.event("entity_dead", vid=vid, vnum=self.chars[vid].get("race"))

    def _on_script(self, raw: bytes) -> None:
        text = raw.split(b"\0", 1)[0].decode(self.profile.encoding, errors="replace")
        # Görev mektubu (ekranın solundaki bildirim) diyalog değildir: açık diyaloğu ezmemeli
        letter = re.fullmatch(r"\s*\[QUESTBUTTON idx;(\d+)\|name;([^\]]*)\]\s*(\[DONE\])?\s*", text)
        if letter:
            self.quest_letters[int(letter.group(1))] = letter.group(2).strip()
            self.event("quest_letter", index=int(letter.group(1)), title=letter.group(2).strip())
            return
        options: list[str] = []
        answers: list[int] = []
        q = re.search(r"\[QUESTION ([^\]]*)\]", text)
        if q:
            # Gerçek istemci seçeneğin sırasını (0'dan) gönderir; metindeki "1;" numarası değil
            for i, part in enumerate(q.group(1).split("|")):
                _, _, label = part.partition(";")
                options.append((label or part).strip())
                answers.append(i)
        elif "[NEXT]" in text:
            options, answers = ["Devam"], [self.b["script_close_answer"]]
        clean = re.sub(r"\[[^\]]*\]", " ", text)
        clean = " ".join(clean.split())
        # Kapatma (ESC) gerçek istemcideki gibi: soru → son seçeneğin sırası, [NEXT] → 254, [DONE] → hiçbir şey
        close = answers[-1] if q else (self.b["script_close_answer"] if "[NEXT]" in text else None)
        self.dialog = {"text": clean, "options": options or ["Kapat"], "answers": answers or [None],
                       "close": close}
        self.event("window_opened", name="dialog")

    def _on_whisper(self, g: Callable[..., Any], raw: bytes) -> None:
        text = raw.split(b"\0", 1)[0].decode(self.profile.encoding, errors="replace")
        wt = self.b["whisper_types"]
        kind, sender = g("type", 0), g("from", "")
        if kind == wt["NOT_EXIST"]:
            self.message(f"{sender} adlı oyuncu çevrimiçi değil")
            return
        if kind in (wt["TARGET_BLOCKED"], wt["SENDER_BLOCKED"], wt["ERROR"]):
            self.message(f"Fısıltı gönderilemedi ({sender})")
            return
        self._wsp_seq += 1
        self.whispers.append({"seq": self._wsp_seq, "t": self.now_ms(), "from": sender, "text": text,
                              "gm": kind == wt["GM"]})
        del self.whispers[:-200]
        self.event("whisper_received", sender=sender)

    # ------------------------------------------------------------------ Metin2Re görev motoru (MRQ_*)
    @staticmethod
    def _dehex(h: str) -> str:
        if not h or h == "-":
            return ""
        try:
            return bytes.fromhex(h).decode("cp1254", errors="replace")
        except ValueError:
            return ""

    def _on_mrq(self, text: str) -> None:
        """mr2_qlib.lua'nın istemciye gönderdiği görev komutları (client/Client/mr2quest.py ile aynı biçim)."""
        a = text.split()
        cmd, args = a[0], a[1:]
        try:
            if cmd == "MRQ_Q" and len(args) >= 5:
                qid = args[0]
                q = self.quests.setdefault(qid, {"objs": {}, "mark": None})
                desc = self._dehex(args[4])
                if q.get("desc") != desc:
                    q["objs"] = {}
                q.update(status=int(args[1]), turnin=int(args[2]), title=self._dehex(args[3]), desc=desc)
                self.quests_done.discard(qid)
                self.quests_available.pop(qid, None)
            elif cmd == "MRQ_O" and len(args) >= 5 and args[0] in self.quests:
                self.quests[args[0]]["objs"][int(args[1])] = (int(args[2]), int(args[3]), self._dehex(args[4]))
            elif cmd == "MRQ_M" and len(args) >= 6 and args[0] in self.quests:
                self.quests[args[0]]["mark"] = tuple(int(v) for v in args[1:6])
            elif cmd == "MRQ_U" and args and args[0] in self.quests:
                self.quests[args[0]]["mark"] = None
            elif cmd == "MRQ_D" and args:
                if self.quests.pop(args[0], None) is not None:
                    self.quests_done.add(args[0])
            elif cmd == "MRQ_A" and len(args) >= 6:
                self.quests_available[args[0]] = {
                    "npc": int(args[1]), "vid": int(args[2]), "x": int(args[3]), "y": int(args[4]),
                    "gate": bool(int(args[5])), "title": self._dehex(args[6]) if len(args) > 6 else ""}
            elif cmd == "MRQ_AX" and args:
                self.quests_available.pop(args[0], None)
        except ValueError:
            self.log(f"MRQ ayrıştırılamadı: {text}")

    # ------------------------------------------------------------------ ticaret (CG/GC_EXCHANGE)
    def _on_shop(self, g: Callable[..., Any], raw: bytes) -> None:
        subs = self.b["shop_subheaders"]
        sub = g("sub")
        st = self.b["shop_item_struct"]
        if sub == subs["GC_START"]:
            size, n = self.profile.sizeof(st), self.b["shop_max_items"]
            off = max(0, len(raw) - size * n)      # sunucu önce DWORD owner_vid gönderir
            owner = int.from_bytes(raw[:4], "little") if off >= 4 else self._last_click
            items = []
            for i in range(n):
                if off + size > len(raw):
                    break
                it, off = self.profile.decode(st, raw, off)
                if it.get("vnum"):
                    items.append({"slot": i, "vnum": it["vnum"], "price": it.get("price", 0),
                                  "count": it.get("count", 1)})
            self.shop = {"npc_vid": owner or self._last_click, "items": items}
            self.event("window_opened", name="shop")
        elif sub == subs["GC_END"]:
            self.shop = None
        elif sub == subs["GC_UPDATE_ITEM"] and self.shop is not None and len(raw) > 1:
            it, _ = self.profile.decode(st, raw, 1)
            rest = [i for i in self.shop["items"] if i["slot"] != raw[0]]
            if it.get("vnum"):
                rest.append({"slot": raw[0], "vnum": it["vnum"], "price": it.get("price", 0),
                             "count": it.get("count", 1)})
            self.shop["items"] = sorted(rest, key=lambda i: i["slot"])
        else:
            names = {v: k for k, v in subs.items() if k.startswith("GC_")}
            self._shop_result = names.get(sub, str(sub))

    def _xsub(self, key: str) -> int:
        return int(self.b["exchange_subheaders"][key])

    def _on_exchange(self, g: Callable[..., Any]) -> None:
        sub, is_me = g("sub"), bool(g("is_me", 0))
        gc = {k: int(v) for k, v in self.b["exchange_subheaders"].items() if k.startswith("GC_")}
        if sub == gc["GC_START"]:
            vid = int(g("arg1", 0))
            partner = self.chars.get(vid, {})
            if not self._trade_allowed(partner.get("name", "")):
                # Normal oyuncularla ticaret yasak: gelen isteği hemen kapat
                self.log(f"ticaret reddedildi: {partner.get('name') or vid} izinli değil")
                self.event("trade_refused", partner_vid=vid, partner_name=partner.get("name", ""))
                self.trade = None                  # sonraki GC_EXCHANGE paketleri yok sayılır
                self._send_exchange("CG_CANCEL")
                return
            self.trade = {"partner_vid": vid, "partner_name": partner.get("name", ""), "my_items": {},
                          "their_items": {}, "my_gold": 0, "their_gold": 0, "my_accepted": False,
                          "their_accepted": False}
            self.event("trade_started", partner_vid=vid, partner_name=partner.get("name", ""))
            return
        t = self.trade
        if sub == gc["GC_ALREADY"]:
            self.message("Oyuncu zaten ticarette")
            return
        if sub == gc["GC_LESS_ELK"]:
            self.message("Yeterli Yang yok.")
            return
        if t is None:
            return
        side = "my" if is_me else "their"
        if sub == gc["GC_ITEM_ADD"]:
            pos = g("arg2") or {}
            cell = pos.get("cell", 0) if isinstance(pos, dict) else int(pos or 0)
            t[f"{side}_items"][cell] = {"vnum": int(g("arg1", 0)), "count": int(g("arg3", 1))}
            t["my_accepted"] = t["their_accepted"] = False
            self.event("trade_updated")
        elif sub == gc["GC_ITEM_DEL"]:
            t[f"{side}_items"].pop(int(g("arg1", 0)), None)
            t["my_accepted"] = t["their_accepted"] = False
            self.event("trade_updated")
        elif sub == gc["GC_ELK_ADD"]:
            t[f"{side}_gold"] = int(g("arg1", 0))
            t["my_accepted"] = t["their_accepted"] = False
            self.event("trade_updated")
        elif sub == gc["GC_ACCEPT"]:
            t[f"{side}_accepted"] = bool(g("arg1", 0))
            self.event("trade_updated")
        elif sub == gc["GC_END"]:
            done = t["my_accepted"] and t["their_accepted"]
            self.trade = None
            if done:
                self.event("trade_completed")
            else:
                self.event("trade_cancelled", reason="CANCELLED")
            t["completed"] = done
            self._last_trade = t

    def _trade_allowed(self, name: str) -> bool:
        allowed = [n.lower() for n in (self.cfg.trade_partners or [])]
        return not allowed or (name or "").lower() in allowed

    def _need_trade(self) -> dict[str, Any]:
        if self.trade is None:
            raise HeadlessError("NO_TRADE", "Açık ticaret yok")
        return self.trade

    def _send_exchange(self, key: str, arg1: int = 0, arg2: int = 0, pos: dict[str, Any] | None = None) -> None:
        self._send("exchange", sub=self._xsub(key), arg1=int(arg1), arg2=int(arg2),
                   pos=pos or {"window_type": 0, "cell": 0xFFFF})

    def _trade_view(self) -> dict[str, Any]:
        t = self.trade or {}
        items = lambda d: [{"slot": c, "vnum": v["vnum"], "count": v["count"], "name": str(v["vnum"])}  # noqa: E731
                           for c, v in sorted(d.items())]
        return {"partner_vid": t.get("partner_vid"), "partner_name": t.get("partner_name"),
                "my_items": items(t.get("my_items", {})), "their_items": items(t.get("their_items", {})),
                "my_gold": t.get("my_gold", 0), "their_gold": t.get("their_gold", 0),
                "my_accepted": t.get("my_accepted", False), "their_accepted": t.get("their_accepted", False)}

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
            if d <= step and self.route:
                # ara noktaya varıldı: durmadan sıradakine dön
                self.me["x"], self.me["y"] = self.dest
                self.dest = self.route.pop(0)
                self._send("move", func=funcs["MOVE"], arg=0, rot=rot, x=int(self.me["x"]), y=int(self.me["y"]),
                           time=self.game_time_ms())
            elif d <= step:
                self.me["x"], self.me["y"] = self.dest
                self.dest = None
                self._send("move", func=funcs["WAIT"], arg=0, rot=rot, x=int(self.me["x"]), y=int(self.me["y"]),
                           time=self.game_time_ms())
            else:
                self.me["x"] += dx / d * step
                self.me["y"] += dy / d * step
                self._send("move", func=funcs["MOVE"], arg=0, rot=rot, x=int(self.me["x"]), y=int(self.me["y"]),
                           time=self.game_time_ms())
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
                "attacking": self.attacking, "target_vid": self.target,
                "skills": {str(v): lv for v, lv in sorted(self.skills.items())}}

    def cmd_get_inventory(self) -> dict[str, Any]:
        self._need_game()
        size = self.b["inventory_size"]
        from ..engine.npcdir import item_name

        items = [{"slot": c, "vnum": v["vnum"], "count": v["count"], "name": item_name(v["vnum"])}
                 for c, v in sorted(self.items.items()) if c < size]
        wear = self.b["wear_names"]
        eq = {wear.get(c - size, f"wear{c - size}"): {"vnum": v["vnum"], "name": item_name(v["vnum"])}
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
        out = [{"name": "dialog", "text": self.dialog["text"], "options": self.dialog["options"]}] if self.dialog else []
        if self.shop is not None:
            from ..engine.npcdir import item_name

            out.append({"name": "shop", "npc_vid": self.shop["npc_vid"],
                        "items": [{**i, "name": item_name(i["vnum"])} for i in self.shop["items"]]})
        if self.refine_window is not None:
            out.append({"name": "refine", **self.refine_window})
        if self.trade is not None:
            out.append({"name": "trade", **self._trade_view()})
        if self.quest_letters:
            out.append({"name": "quest_letters", "letters": [{"index": i, "title": t}
                                                             for i, t in sorted(self.quest_letters.items())]})
        return out

    def cmd_get_quest_state(self) -> dict[str, Any]:
        """Metin2Re görev motoru durumu (QA istemci köprüsüyle aynı biçim; koordinatlar dünya koordinatı).
        Alınabilir görevler state="available" ve veren NPC hedefiyle döner."""
        out: dict[str, Any] = {}
        for qid, a in self.quests_available.items():
            out[qid] = {"state": "available", "title": a["title"], "progress": 0, "objs": [], "marked": True,
                        "target": {"npc": a["npc"], "vid": a["vid"], "x": a["x"], "y": a["y"], "gate": a["gate"]}}
        for qid, q in self.quests.items():
            objs = [{"label": label, "cur": cur, "max": mx} for _, (cur, mx, label) in sorted(q["objs"].items())]
            entry = {"state": "ready" if q.get("status") == 2 else "active", "title": q.get("title", ""),
                     "progress": sum(min(o["cur"], o["max"]) for o in objs), "objs": objs,
                     "marked": q.get("mark") is not None}
            if q.get("mark"):
                npc, vid, x, y, gate = q["mark"]
                entry["target"] = {"npc": npc, "vid": vid, "x": x, "y": y, "gate": bool(gate)}
            out[qid] = entry
        for qid in self.quests_done:
            out.setdefault(qid, {"state": "done", "title": "", "progress": 0, "objs": [], "marked": False})
        return out

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
        self._select_and_enter(index)
        self.event("map_loaded", map=self._map_index())
        return {"name": self.me["name"], "map": self._map_index()}

    def cmd_logout(self) -> dict[str, Any]:
        self._need_game()
        self._leave_cleanly()
        self._drop_connection()
        self.account = None
        return {}

    # ------------------------------------------------------------------ aksiyonlar
    def _nav(self, m: dict[str, Any] | None) -> Any:
        if m is None or not m.get("file"):
            return None
        if not hasattr(self, "_navstore"):
            from .navmesh import NavStore

            root = Path(self.cfg.nav_dir)
            if not root.is_absolute() and hasattr(self, "_resolve"):
                root = Path(self._resolve(str(root)))
            self._navstore = NavStore(root)
        return self._navstore.grid(m["file"], int(m["x"]), int(m["y"]))

    def _plan_route(self, x: float, y: float, m: dict[str, Any] | None) -> list[tuple[float, float]]:
        """Sunucunun yürünebilirlik verisiyle engellerin etrafından dolaşan ara noktalar (normal oyuncu gibi;
        duvardan ya da binadan geçmez). Veri yoksa düz çizgi."""
        grid = self._nav(m)
        if grid is None:
            return [(x, y)]
        from .navmesh import NavError

        try:
            return grid.find_path((self.me["x"], self.me["y"]), (x, y))
        except NavError as e:
            raise HeadlessError("NO_PATH", str(e)) from e

    MAP_EDGE_MARGIN = 300    # harita kenarına bu kadar yaklaşma (sunucu: "Sync: cannot find tree" → bağlantı kesilir)

    def _current_map(self) -> dict[str, Any] | None:
        x, y = self.me["x"], self.me["y"]
        return next((m for m in self.cfg.maps
                     if m["x"] <= x < m["x"] + m["width"] and m["y"] <= y < m["y"] + m["height"]), None)

    def cmd_move_to(self, x: float, y: float) -> dict[str, Any]:
        self._need_alive()
        m = self._current_map()
        if m is not None:
            e = self.MAP_EDGE_MARGIN
            x0, y0, x1, y1 = m["x"] + e, m["y"] + e, m["x"] + m["width"] - e, m["y"] + m["height"] - e
            if not (x0 <= float(x) <= x1 and y0 <= float(y) <= y1):
                # Gerçek istemci harita dışına yürüyemez; sunucu da haritadan çıkan karakteri atar
                raise HeadlessError("OUT_OF_MAP", f"Hedef harita dışında: harita {m['index']} sınırları "
                                    f"X {int(x0)}..{int(x1)}, Y {int(y0)}..{int(y1)}")
        route = self._plan_route(float(x), float(y), m)
        npc = self.chars.get(self.shop["npc_vid"]) if self.shop is not None else None
        if self.shop is not None and (npc is None or _dist(float(x), float(y), npc["x"], npc["y"]) > 1000):
            self._close_shop()      # gerçek istemci satıcıdan uzaklaşınca dükkânı kapatır
        self.dest, self.route = route[0], route[1:]
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
        self.dest, self.route = None, []
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
        if self.dialog is not None:
            # Oyuncu gibi: açık görev penceresini kapat. Cevap bekleyen görev açık kaldıkça sunucu o görevin
            # başka NPC'lerdeki seçeneklerini menüye koymaz.
            self.log("açık diyalog kapatıldı (yeni NPC'ye tıklamadan önce)")
            self._close_dialog()
        if self.shop is not None:
            # Gerçek istemci başka NPC'ye tıklayınca/uzaklaşınca dükkânı kapatır (SendShopEndPacket); açık dükkân
            # varken sunucu yeni dükkân açmaz
            self._close_shop()
        self._last_click = int(vid)
        self._send("on_click", vid=int(vid))
        self._pump(300)
        return {"window": "shop" if self.shop is not None else "dialog" if self.dialog else None}

    def cmd_select_dialog(self, index: int) -> dict[str, Any]:
        self._need_alive()
        d = self.dialog
        if not d:
            raise HeadlessError("NO_DIALOG", "Açık dialog yok")
        if not 0 <= int(index) < len(d["options"]):
            raise HeadlessError("BAD_OPTION", "Geçersiz seçenek")
        self.dialog = None
        answer = d["answers"][int(index)]
        if answer is not None:           # [DONE] penceresinin "Kapat"ı sunucuya bir şey göndermez
            self._send("script_answer", answer=answer)
        self._pump(200)
        return {"selected": d["options"][int(index)]}

    def _close_dialog(self) -> None:
        d, self.dialog = self.dialog, None
        if d is not None and d.get("close") is not None:
            self._send("script_answer", answer=d["close"])
            self._pump(200)

    def cmd_close_window(self, name: str) -> dict[str, Any]:
        if name == "dialog" and self.dialog:
            self._close_dialog()
        elif name == "shop" and self.shop is not None:
            self._close_shop()
        elif name == "refine" and self.refine_window is not None:
            self._cancel_refine()
        return {}

    # ------------------------------------------------------------------ dükkân
    def _close_shop(self) -> None:
        self.shop = None
        self._send("shop", sub=self.b["shop_subheaders"]["CG_END"])
        self._pump(150)

    SHOP_ERRORS = {"GC_NOT_ENOUGH_MONEY": ("NOT_ENOUGH_GOLD", "Yetersiz yang"),
                   "GC_INVENTORY_FULL": ("INVENTORY_FULL", "Envanter dolu"),
                   "GC_SOLDOUT": ("SOLD_OUT", "Tükendi"), "GC_INVALID_POS": ("BAD_SLOT", "Geçersiz dükkân slotu")}

    def cmd_buy_item(self, slot: int) -> dict[str, Any]:
        self._need_alive()
        if self.shop is None:
            raise HeadlessError("NO_SHOP", "Açık dükkân yok (önce satıcı NPC ile konuş)")
        it = next((i for i in self.shop["items"] if i["slot"] == int(slot)), None)
        if it is None:
            raise HeadlessError("BAD_SLOT", f"Dükkânda {slot}. slot boş")
        gold0 = self._point("GOLD", 0) or 0
        self._shop_result = None
        # Başarılı alımda sunucu yanıt paketi göndermez (shop_manager.cpp Buy): yang/envanter değişimini bekle
        self._send("shop", trailing=bytes([1, int(slot)]), sub=self.b["shop_subheaders"]["CG_BUY"])
        self._pump_for(lambda: self._shop_result is not None or (self._point("GOLD", 0) or 0) != gold0, 2.0)
        self._pump(150)
        if self._shop_result in self.SHOP_ERRORS:
            raise HeadlessError(*self.SHOP_ERRORS[self._shop_result])
        gold = self._point("GOLD", 0) or 0
        if gold == gold0:
            # Sunucu ne hata ne yang değişimi gönderdi: alım olmadı (ör. dükkân kapanmış) — başarı sayma
            raise HeadlessError("BUY_FAILED", "Satın alma gerçekleşmedi (yang değişmedi); dükkânı yeniden aç")
        return {"vnum": it["vnum"], "price": it["price"], "gold": gold, "confirmed": True}

    def cmd_sell_item(self, slot: int, count: int | None = None) -> dict[str, Any]:
        self._need_alive()
        if self.shop is None:
            raise HeadlessError("NO_SHOP", "Açık dükkân yok (önce satıcı NPC ile konuş)")
        slot = int(slot)
        self._item_pos(slot)
        have = int(self.items[slot].get("count") or 1)
        n = have if count is None else int(count)
        if not 0 < n <= have:
            raise HeadlessError("BAD_COUNT", "Geçersiz adet")
        gold0, since = self._point("GOLD", 0) or 0, self._msg_seq
        self._send("shop", trailing=bytes([slot, n]), sub=self.b["shop_subheaders"]["CG_SELL2"])
        self._pump_for(lambda: (self._point("GOLD", 0) or 0) != gold0, 2.0)
        gold = self._point("GOLD", 0) or 0
        if gold == gold0:
            why = self._msgs_since(since)
            raise HeadlessError("SELL_REFUSED", "Satış olmadı" + (f": {why[-1]}" if why else ""))
        return {"gold": gold, "price": gold - gold0}

    # ------------------------------------------------------------------ yükseltme (+)
    def _cancel_refine(self) -> None:
        w, self.refine_window = self.refine_window, None
        if w is not None:
            self._send("refine", pos=int(w["slot"]), type=self.b["refine_cancel_type"])
            self._pump(100)

    def _nearest_blacksmith(self) -> int:
        smiths = [c for c in self.chars.values() if c["type"] == "npc" and c["race"] in self.b["blacksmith_vnums"]]
        if not smiths:
            raise HeadlessError("NO_BLACKSMITH", "Yakında demirci yok (köydeki demirciye yürü)")
        return min(smiths, key=lambda c: _dist(self.me["x"], self.me["y"], c["x"], c["y"]))["vid"]

    def cmd_refine_item(self, slot: int, npc_vid: int | None = None, scroll_slot: int | None = None,
                        confirm: bool = True) -> dict[str, Any]:
        """Normal oyuncu gibi yükselt: eşyayı demirciye ver (ya da kâğıdı eşyaya sürükle), sunucunun
        açtığı pencereyi (ücret, şans, malzeme) gör ve onayla. confirm=False yalnız bilgiyi gösterir."""
        self._need_alive()
        slot = int(slot)
        pos = self._item_pos(slot)
        if self.dialog is not None:
            self._close_dialog()
        self.refine_window = None
        since = self._msg_seq
        if scroll_slot is not None:
            self._send("item_use_to_item", source=self._item_pos(int(scroll_slot)), target=pos)
        else:
            vid = int(npc_vid) if npc_vid is not None else self._nearest_blacksmith()
            self._in_range(self._entity(vid), self.b["interact_range"])
            self._send("give_item", vid=vid, pos=pos, count=1)
        if not self._pump_for(lambda: self.refine_window is not None, 3.0):
            why = self._msgs_since(since)
            raise HeadlessError("REFINE_REFUSED", "Yükseltme penceresi açılmadı" + (f": {why[-1]}" if why else
                                " (eşya yükseltilemiyor olabilir)"))
        info = {k: v for k, v in self.refine_window.items() if k != "slot"}
        if not confirm:
            self._cancel_refine()
            return {"confirmed": False, **info}
        gold = self._point("GOLD", 0) or 0
        lacking = [m for m in info["materials"]
                   if sum(v["count"] for c, v in self.items.items() if v["vnum"] == m["vnum"] and c < self.b["inventory_size"])
                   < (m["count"] or 0)]
        if (info["cost"] or 0) > gold or lacking:
            self._cancel_refine()
            if lacking:
                raise HeadlessError("MISSING_MATERIALS", f"Eksik malzeme: {lacking}")
            raise HeadlessError("NOT_ENOUGH_GOLD", f"Yükseltme ücreti {info['cost']} yang, sende {gold}")
        w, self.refine_window = self.refine_window, None
        since = self._msg_seq
        self._send("refine", pos=int(w["slot"]), type=int(w["type"] or 0))
        done = lambda: any(t in ("RefineSuceeded", "RefineFailed") for t in self._msgs_since(since))  # noqa: E731
        self._pump_for(done, 3.0)
        self._pump(150)
        msgs = self._msgs_since(since)
        if "RefineSuceeded" in msgs:
            result = "success"
        elif "RefineFailed" in msgs:
            result = "failed"
        else:
            raise HeadlessError("REFINE_REJECTED", "Sunucu yükseltmeyi yapmadı" + (f": {msgs[-1]}" if msgs else ""))
        return {"confirmed": True, "result": result, **info, "slot_vnum": self.items.get(slot, {}).get("vnum"),
                "gold": self._point("GOLD", 0)}

    # ------------------------------------------------------------------ beceri / ekipman / eşya bırakma
    def cmd_use_skill(self, slot: int) -> dict[str, Any]:
        """slot = beceri vnum'u (get_player_state.skills). Gerçek istemci gibi önce CG_USE_SKILL, hedefli
        saldırı becerisinde ardından beceri türlü tek vuruş (sunucu vuruş sayısını kullanım başına sınırlar)."""
        self._need_alive()
        vnum = int(slot)
        if self.skills and self.skills.get(vnum, 0) <= 0:
            raise HeadlessError("SKILL_NOT_LEARNED", f"Beceri {vnum} öğrenilmemiş (öğrenilenler: "
                                f"{sorted(self.skills)})")
        since = self._msg_seq
        t = self.chars.get(self.target) if self.target else None
        self._send("use_skill", vnum=vnum, vid=int(t["vid"]) if t else 0)
        self._pump(250)
        if t is not None and t["type"] == "monster" and not t.get("dead") \
                and _dist(self.me["x"], self.me["y"], t["x"], t["y"]) <= self.b["attack_range"]:
            self._send("attack", type=vnum, vid=t["vid"])
            self._pump(150)
        return {"skill": vnum, "target_vid": t["vid"] if t else None, "messages": self._msgs_since(since)[-3:]}

    def cmd_unequip_item(self, wear_slot: str) -> dict[str, Any]:
        self._need_alive()
        wear = [k for k, v in self.b["wear_names"].items() if v == wear_slot]
        if not wear:
            raise HeadlessError("BAD_ARGS", f"Bilinmeyen ekipman yeri: {wear_slot}")
        cell = self.b["inventory_size"] + int(wear[0])
        if cell not in self.items:
            raise HeadlessError("EMPTY_SLOT", "Ekipman yeri boş")
        vnum = self.items[cell]["vnum"]
        self._send("item_use", pos={"window_type": self.b["inventory_window"], "cell": cell})  # giyiliye sağ tık
        self._pump_for(lambda: cell not in self.items, 1.5)
        if cell in self.items:
            raise HeadlessError("UNEQUIP_FAILED", "Çıkarılamadı (envanter dolu olabilir)")
        slot = next((c for c, v in sorted(self.items.items()) if v["vnum"] == vnum and c < self.b["inventory_size"]),
                    None)
        return {"slot": slot}

    def cmd_split_item(self, slot: int, count: int) -> dict[str, Any]:
        """Yığını böl: count kadarını ilk boş envanter hücresine taşı (istemcide Shift+sürükle, CG_ITEM_MOVE)."""
        self._need_alive()
        slot, count = int(slot), int(count)
        pos = self._item_pos(slot)
        have = int(self.items[slot].get("count") or 1)
        if not 0 < count < have:
            raise HeadlessError("BAD_COUNT", f"Bölmek için 1..{have - 1} arası adet gerekli (yığında {have} var)")
        size = self.b["inventory_size"]
        free = next((c for c in range(size) if c not in self.items), None)
        if free is None:
            raise HeadlessError("INVENTORY_FULL", "Envanterde boş yer yok")
        vnum = self.items[slot]["vnum"]
        self._send("item_move", pos=pos, to={"window_type": self.b["inventory_window"], "cell": free}, count=count)
        if not self._pump_for(lambda: self.items.get(free, {}).get("vnum") == vnum, 2.0):
            raise HeadlessError("SPLIT_FAILED", "Yığın bölünemedi")
        return {"slot": free, "count": self.items[free]["count"]}

    def cmd_drop_item(self, slot: int, count: int | None = None) -> dict[str, Any]:
        self._need_alive()
        slot = int(slot)
        pos = self._item_pos(slot)
        have = int(self.items[slot].get("count") or 1)
        n = have if count is None else int(count)
        if not 0 < n <= have:
            raise HeadlessError("BAD_COUNT", "Geçersiz adet")
        vnum = self.items[slot]["vnum"]
        self._send("item_drop", pos=pos, gold=0, count=n)
        self._pump(300)
        return {"vnum": vnum, "count": n}

    # ------------------------------------------------------------------ ticaret
    def cmd_trade_request(self, vid: int) -> dict[str, Any]:
        self._need_alive()
        if self.trade is not None:
            raise HeadlessError("ALREADY_TRADING", "Zaten ticaretteyim")
        e = self.chars.get(int(vid))
        if e is None or e.get("type") != "pc":
            raise HeadlessError("NO_ENTITY", f"Oyuncu bulunamadı (VID {vid})")
        if not self._trade_allowed(e.get("name", "")):
            raise HeadlessError("TRADE_FORBIDDEN", f"{e.get('name') or vid} ile ticaret yasak (yalnız: "
                                f"{', '.join(self.cfg.trade_partners)})")
        self._in_range(e, self.b["trade_range"])
        since = self._msg_seq
        self._send_exchange("CG_START", arg1=int(vid))
        deadline = time.monotonic() + 3.0
        while self.trade is None and time.monotonic() < deadline and self.conn is not None:
            self._pump(100)
        if self.trade is None:
            why = [m["text"] for m in self.messages if m["seq"] > since]
            raise HeadlessError("TRADE_REFUSED", "Ticaret açılmadı" + (f": {why[-1]}" if why else
                                " (oyuncu meşgul, ticareti engellemiş ya da çok uzakta olabilir)"))
        return {"partner": self.trade.get("partner_name") or e.get("name", "")}

    def cmd_trade_add_item(self, slot: int) -> dict[str, Any]:
        t = self._need_trade()
        slot = int(slot)
        self._item_pos(slot)
        if any(v.get("_slot") == slot for v in t["my_items"].values()):
            raise HeadlessError("ALREADY_ADDED", "Item zaten eklendi")
        used = set(t["my_items"])
        free = next((i for i in range(self.b["trade_max_items"]) if i not in used), None)
        if free is None:
            raise HeadlessError("TRADE_FULL", "Ticaret penceresi dolu")
        self._send_exchange("CG_ITEM_ADD", arg2=free, pos=self._item_pos(slot))
        self._pump(300)
        if free in t["my_items"]:
            t["my_items"][free]["_slot"] = slot
        return {}

    def cmd_trade_set_gold(self, amount: int) -> dict[str, Any]:
        self._need_trade()
        amount = int(amount)
        if amount < 0 or amount > (self._point("GOLD", 0) or 0):
            raise HeadlessError("NOT_ENOUGH_GOLD", "Yetersiz yang")
        self._send_exchange("CG_ELK_ADD", arg1=amount)
        self._pump(300)
        return {}

    def cmd_trade_accept(self) -> dict[str, Any]:
        self._need_trade()
        self._last_trade = None
        self._send_exchange("CG_ACCEPT")
        self._pump(600)
        last = getattr(self, "_last_trade", None)
        if last is not None:
            return {"completed": bool(last.get("completed")), "cancelled": None if last.get("completed") else "CANCELLED"}
        return {"completed": False, "cancelled": None}

    def cmd_trade_cancel(self) -> dict[str, Any]:
        self._need_trade()
        self._send_exchange("CG_CANCEL")
        self._pump(300)
        return {}

    def cmd_get_whispers(self, since: int = 0) -> list[dict[str, Any]]:
        if self.conn is not None:
            self._pump(0)
        return [w for w in self.whispers if w["seq"] > since]

    def cmd_whisper(self, to: str, message: str) -> dict[str, Any]:
        self._need_game()
        raw = str(message).encode(self.profile.encoding, errors="replace")[:500] + b"\0"
        since = self._msg_seq
        self._send("whisper", trailing=raw, to=str(to))
        self._pump(200)
        err = [m["text"] for m in self.messages if m["seq"] > since and str(to) in m["text"]]
        if err:
            raise HeadlessError("WHISPER_FAILED", err[-1])
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
