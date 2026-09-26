"""Sahte Metin2 auth + game sunucusu (binary protokol) — headless client'ı gerçek sunucu olmadan sınamak için.

Profil ne diyorsa o paketleri konuşur (header numaraları, struct'lar, dinamik boyutlar). Minik bir dünya:
bir oyuncu, bir mob (3 vuruşta ölür, drop bırakır), bir NPC (görev diyaloğu), /qa hazırlık komutları.
Ayrıca oyuncunun hareketini denetler: tek adımda çok uzağa "ışınlanma" (hız hilesi) ihlal olarak sayılır.

`improved=True`: gerçek sunucudaki gibi handshake'ten sonra DH2 anahtar anlaşması yapar ve
KEY_AGREEMENT_COMPLETED'dan sonra iki yönü şifreler. Profilde SEQUENCE tablosu varsa gelen her
SEQUENCE paketinin son baytını denetler (yanlışsa `seq_errors` artar, gerçek sunucu bağlantıyı keser).
"""

from __future__ import annotations

import socketserver
import threading
import time
from typing import Any

from .crypto import ImprovedCrypto, ImprovedKeyAgreement
from .net import PacketConnection, ProtocolError
from .profile import Profile

HANDSHAKE = 0x1234ABCD
LOGIN_KEY = 777
MOB_VID, NPC_VID, ME_VID = 200, 300, 100
PC_VID = 500          # improved profilde: ticareti kabul eden gerçek oyuncu (TESTR)
MAX_STEP = 400  # tek MOVE paketinde izin verilen en büyük mesafe (hız kontrolü)


class FakeWorld:
    def __init__(self, password: str = "qa"):
        self.password = password
        self.lock = threading.Lock()
        self.stats = {"moves": 0, "speed_violations": 0, "attacks": 0, "pongs": 0, "logins": 0,
                      "key_agreements": 0, "seq_packets": 0, "seq_errors": 0}


class _Handler(socketserver.BaseRequestHandler):
    server: "_Server"

    def setup(self) -> None:
        self.c = PacketConnection.from_socket(self.server.profile, self.request, "CG", "GC")
        self.w = self.server.world
        self.phase_consts = self.server.profile.constants
        self.hp = 3
        self.me = {"x": 5000, "y": 5000, "gold": 0, "hp": 400, "items": {0: [27001, 3]}}
        self.name = ""
        self.seq_index = 0
        self.quest_step = ""
        self.pending = None
        self.trade = None
        self.key_agreement: ImprovedKeyAgreement | None = None

    def send(self, header: str, **v: Any) -> None:
        self.c.send(header, v)

    def phase(self, name: str) -> None:
        self.send("HEADER_GC_PHASE", phase=self.phase_consts[name])

    def chat(self, text: str, ctype: int = 1) -> None:
        self.c.send("HEADER_GC_CHAT", {"type": ctype, "id": 0, "bEmpire": 0}, text.encode("cp1254") + b"\0")

    def point(self, t: int, value: int) -> None:
        self.send("HEADER_GC_CHARACTER_POINT_CHANGE", dwVID=ME_VID, type=t, amount=0, value=value)

    def item(self, cell: int, vnum: int, count: int) -> None:
        self.send("HEADER_GC_ITEM_SET", Cell={"window_type": 1, "cell": cell}, vnum=vnum, count=count)

    def handle(self) -> None:
        self.send("HEADER_GC_HANDSHAKE", dwHandshake=HANDSHAKE, dwTime=int(time.time()), lDelta=0)
        try:
            while True:
                for p in self.c.poll(0.2):
                    if p.seq is not None and not self.check_sequence(p.seq):
                        return   # gerçek sunucu: "SEQUENCE mismatch" → PHASE_CLOSE
                    self.on_packet(p.name, p.data, p.trailing)
        except ProtocolError:
            pass

    def check_sequence(self, got: int) -> bool:
        table = self.server.profile.sequence
        want = table[self.seq_index]
        self.seq_index = (self.seq_index + 1) % len(table)
        with self.w.lock:
            self.w.stats["seq_packets"] += 1
            if got != want:
                self.w.stats["seq_errors"] += 1
        return got == want

    def on_packet(self, name: str, d: dict[str, Any], trailing: bytes) -> None:
        auth = self.server.kind == "auth"
        if name == "HEADER_CG_HANDSHAKE":
            if d.get("dwHandshake") == HANDSHAKE:
                if self.server.improved:
                    self.key_agreement = ImprovedKeyAgreement()
                    data = self.key_agreement.data
                    self.send("HEADER_GC_KEY_AGREEMENT", wAgreedLength=256, wDataLength=len(data), data=data)
                else:
                    self.phase("PHASE_AUTH" if auth else "PHASE_LOGIN")
        elif name == "HEADER_CG_KEY_AGREEMENT" and self.key_agreement is not None:
            # önce COMPLETED düz gider, sonra iki yön şifreli (desc.cpp / input.cpp ile aynı sıra)
            self.send("HEADER_GC_KEY_AGREEMENT_COMPLETED")
            peer = bytes(d["data"])[:d["wDataLength"]]
            self.c.crypto = ImprovedCrypto(self.key_agreement.shared(d["wAgreedLength"], peer), polarity=False)
            self.key_agreement = None
            with self.w.lock:
                self.w.stats["key_agreements"] += 1
            self.phase("PHASE_AUTH" if auth else "PHASE_LOGIN")
        elif name == "HEADER_CG_LOGIN3" and auth:
            if d["passwd"] == self.w.password and d["login"].startswith("AI_QA_"):
                self.send("HEADER_GC_AUTH_SUCCESS", dwLoginKey=LOGIN_KEY, bResult=1)
            else:
                self.send("HEADER_GC_LOGIN_FAILURE", szStatus="WRONGPWD")
        elif name == "HEADER_CG_LOGIN2" and not auth:
            if d["dwLoginKey"] != LOGIN_KEY:
                self.send("HEADER_GC_LOGIN_FAILURE", szStatus="NOID")
                return
            self.name = d["login"]
            with self.w.lock:
                self.w.stats["logins"] += 1
            self.send("HEADER_GC_LOGIN_SUCCESS4", players=[{"dwID": 1, "szName": self.name, "byLevel": 10}])
            self.phase("PHASE_SELECT")
        elif name == "HEADER_CG_CHARACTER_SELECT":
            self.phase("PHASE_LOADING")
            self.send("HEADER_GC_MAIN_CHARACTER", dwVID=ME_VID, wRaceNum=0, szName=self.name, lx=5000, ly=5000, lz=0)
            pts = [0] * 255
            pts[1], pts[3], pts[5], pts[6], pts[7], pts[8], pts[11] = 10, 0, 400, 400, 100, 100, 0
            self.send("HEADER_GC_CHARACTER_POINTS", points=pts)
            self.item(0, 27001, 3)
        elif name == "HEADER_CG_ENTERGAME":
            self.phase("PHASE_GAME")
            self.send("HEADER_GC_CHARACTER_ADD", dwVID=MOB_VID, x=5200, y=5000, bType=0, wRaceNum=101)
            self.send("HEADER_GC_CHAR_ADDITIONAL_INFO", dwVID=MOB_VID, name="Yaban Köpeği")
            self.send("HEADER_GC_CHARACTER_ADD", dwVID=NPC_VID, x=4800, y=5000, bType=1, wRaceNum=9001)
            self.send("HEADER_GC_CHAR_ADDITIONAL_INFO", dwVID=NPC_VID, name="Satıcı")
            if "HEADER_GC_EXCHANGE" in self.server.profile.packets:
                self.send("HEADER_GC_CHARACTER_ADD", dwVID=PC_VID, x=5100, y=5000, bType=6, wRaceNum=0)
                self.send("HEADER_GC_CHAR_ADDITIONAL_INFO", dwVID=PC_VID, name="TESTR")
            self.send("HEADER_GC_PING")
        elif name == "HEADER_CG_PONG":
            with self.w.lock:
                self.w.stats["pongs"] += 1
        elif name == "HEADER_CG_MOVE":
            step = ((d["lX"] - self.me["x"]) ** 2 + (d["lY"] - self.me["y"]) ** 2) ** 0.5
            with self.w.lock:
                self.w.stats["moves"] += 1
                if step > MAX_STEP:
                    self.w.stats["speed_violations"] += 1
            self.me["x"], self.me["y"] = d["lX"], d["lY"]
        elif name == "HEADER_CG_ATTACK":
            with self.w.lock:
                self.w.stats["attacks"] += 1
            if d["dwVID"] == MOB_VID and self.hp > 0:
                self.hp -= 1
                if self.hp == 0:
                    self.send("HEADER_GC_DEAD", vid=MOB_VID)
                    self.send("HEADER_GC_ITEM_GROUND_ADD", x=5210, y=5000, z=0, dwVID=400, dwVnum=30000)
                    self.point(3, 20)
        elif name == "HEADER_CG_ITEM_PICKUP" and d["vid"] == 400:
            self.send("HEADER_GC_ITEM_GROUND_DEL", vid=400)
            self.item(1, 30000, 1)
        elif name == "HEADER_CG_ITEM_USE":
            cell = d["Cell"]["cell"]
            vnum, count = self.me["items"].get(cell, [0, 0])
            if vnum == 27001 and count > 0:
                count -= 1
                self.me["items"][cell] = [vnum, count]
                self.item(cell, vnum if count else 0, count)
                self.me["hp"] = min(400, self.me["hp"] + 50)
                self.point(5, self.me["hp"])
        elif name == "HEADER_CG_ON_CLICK" and d["vid"] == NPC_VID:
            if self.quest_step == "offered":
                # Metin2Re görev motoru: NPC menüsünde görev adı, sonra Kabul/Reddet
                self.script("[QUESTION 1;Yeni Nöbetçi|2;Kapat]")
                self.pending = "menu"
            elif self.quest_step == "active":
                self.script("Aferin![ENTER]Görevi tamamladın.[NEXT]")
                self.pending = "finish"
            else:
                self.script("Merhaba yolcu![ENTER]Ne istersin?[QUESTION 1;Görev ver|2;Hoşça kal][DONE]")
        elif name == "HEADER_CG_EXCHANGE":
            self.on_exchange(d)
        elif name == "HEADER_CG_SCRIPT_ANSWER":
            self.chat(f"cevap {d['answer']}")
            self.on_quest_answer(d["answer"])
        elif name == "HEADER_CG_CHAT":
            msg = trailing.split(b"\0", 1)[0].decode("cp1254")
            parts = msg.split()
            if parts[:1] == ["/qa"] and self.name.startswith("AI_QA_"):
                cmd = parts[1] if len(parts) > 1 else ""
                if cmd == "gold":
                    self.me["gold"] = int(parts[2])
                    self.point(11, self.me["gold"])
                elif cmd == "quest":
                    # görev teklifi: mektup + alınabilir işareti
                    self.quest_step = "offered"
                    self.script("[QUESTBUTTON idx;85|name;Yeni Nöbetçi][DONE]")
                    self.mrq(f"MRQ_A s1_1 9001 {NPC_VID} 4800 5000 0 {'Yeni Nöbetçi'.encode('cp1254').hex().upper()}")
                elif cmd == "hp":
                    self.me["hp"] = int(parts[2])
                    self.point(5, self.me["hp"])
                elif cmd == "warp" and "HEADER_GC_WARP" in self.server.profile.packets:
                    # başka çekirdeğe geçiş: istemci aynı sunucuya yeniden bağlanıp doğrudan oyuna girer
                    self.send("HEADER_GC_WARP", lX=6000, lY=6000, lAddr=0x0100007F,
                              wPort=self.server.server_address[1])
                    return
                self.chat(f"[QA] OK {cmd}")
            else:
                self.chat(f"{self.name} : {msg}", ctype=0)


def _handler_extras() -> None:
    """Görev ve ticaret yardımcıları (improved profil testleri için)."""

    def script(self, text: str) -> None:
        raw = text.encode("cp1254") + b"\0"
        self.c.send("HEADER_GC_SCRIPT", {"skin": 0, "src_size": len(raw)}, raw)

    def mrq(self, text: str) -> None:
        self.chat(text, ctype=5)

    def on_quest_answer(self, answer: int) -> None:
        pending, self.pending = getattr(self, "pending", None), None
        hexs = lambda t: t.encode("cp1254").hex().upper()  # noqa: E731
        if pending == "menu" and answer == 0:
            self.script("Köyü korur musun?[QUESTION 1;Kabul|2;Reddet]")
            self.pending = "accept"
        elif pending == "accept" and answer == 0:
            self.quest_step = "active"
            self.mrq("MRQ_AX s1_1")
            self.mrq(f"MRQ_Q s1_1 1 9001 {hexs('Yeni Nöbetçi')} {hexs('Muhafızla konuş')}")
            self.mrq(f"MRQ_O s1_1 0 0 1 {hexs('Muhafızla konuş')}")
            self.mrq(f"MRQ_M s1_1 9001 {NPC_VID} 4800 5000 0")
        elif pending == "finish" and answer == 254:
            self.quest_step = "done"
            self.mrq("MRQ_D s1_1")
            self.chat("Görev tamamlandı: Yeni Nöbetçi")

    def on_exchange(self, d: dict) -> None:
        sub = d["subheader"]
        pos = {"window_type": 0, "cell": 0}
        ex = lambda s, me, a1=0, a2=None, a3=0: self.send(  # noqa: E731
            "HEADER_GC_EXCHANGE", subheader=s, is_me=me, arg1=a1, arg2=a2 or pos, arg3=a3)
        if sub == 0 and d["arg1"] == PC_VID:
            self.trade = {"items": {}, "gold": 0}
            ex(0, 1, PC_VID)
        elif not getattr(self, "trade", None):
            return
        elif sub == 1:
            cell = d["Pos"]["cell"]
            vnum, count = self.me["items"].get(cell, [0, 0])
            if vnum:
                self.trade["items"][d["arg2"]] = cell
                ex(1, 1, vnum, {"window_type": 0, "cell": d["arg2"]}, count)
        elif sub == 3:
            self.trade["gold"] = d["arg1"]
            ex(3, 1, d["arg1"])
        elif sub == 4:
            ex(4, 1, 1)
            ex(4, 0, 1)          # TESTR de onaylar
            for cell in self.trade["items"].values():
                self.me["items"].pop(cell, None)
                self.item(cell, 0, 0)
            self.me["gold"] -= self.trade["gold"]
            self.point(11, self.me["gold"])
            with self.w.lock:
                self.w.stats["trades"] = self.w.stats.get("trades", 0) + 1
            self.trade = None
            ex(5, 0)
        elif sub == 5:
            self.trade = None
            ex(5, 0)

    for fn in (script, mrq, on_quest_answer, on_exchange):
        setattr(_Handler, fn.__name__, fn)


_handler_extras()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, kind: str, profile: Profile, world: FakeWorld, improved: bool = False):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.kind, self.profile, self.world, self.improved = kind, profile, world, improved


class FakeMetin2:
    """auth + game sunucularını thread'lerde başlatır."""

    def __init__(self, profile: Profile, password: str = "qa", improved: bool = False):
        self.world = FakeWorld(password)
        self.auth = _Server("auth", profile, self.world, improved)
        self.game = _Server("game", profile, self.world, improved)
        for s in (self.auth, self.game):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @property
    def auth_port(self) -> int:
        return self.auth.server_address[1]

    @property
    def game_port(self) -> int:
        return self.game.server_address[1]

    def shutdown(self) -> None:
        for s in (self.auth, self.game):
            s.shutdown()
            s.server_close()
