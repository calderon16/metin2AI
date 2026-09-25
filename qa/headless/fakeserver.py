"""Sahte Metin2 auth + game sunucusu (binary protokol) — headless client'ı gerçek sunucu olmadan sınamak için.

Profil ne diyorsa o paketleri konuşur (header numaraları, struct'lar, dinamik boyutlar). Minik bir dünya:
bir oyuncu, bir mob (3 vuruşta ölür, drop bırakır), bir NPC (görev diyaloğu), /qa hazırlık komutları.
Ayrıca oyuncunun hareketini denetler: tek adımda çok uzağa "ışınlanma" (hız hilesi) ihlal olarak sayılır.
"""

from __future__ import annotations

import socketserver
import threading
import time
from typing import Any

from .net import PacketConnection, ProtocolError
from .profile import Profile

HANDSHAKE = 0x1234ABCD
LOGIN_KEY = 777
MOB_VID, NPC_VID, ME_VID = 200, 300, 100
MAX_STEP = 400  # tek MOVE paketinde izin verilen en büyük mesafe (hız kontrolü)


class FakeWorld:
    def __init__(self, password: str = "qa"):
        self.password = password
        self.lock = threading.Lock()
        self.stats = {"moves": 0, "speed_violations": 0, "attacks": 0, "pongs": 0, "logins": 0}


class _Handler(socketserver.BaseRequestHandler):
    server: "_Server"

    def setup(self) -> None:
        self.c = PacketConnection.from_socket(self.server.profile, self.request, "CG", "GC")
        self.w = self.server.world
        self.phase_consts = self.server.profile.constants
        self.hp = 3
        self.me = {"x": 5000, "y": 5000, "gold": 0, "hp": 400, "items": {0: [27001, 3]}}
        self.name = ""

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
                    self.on_packet(p.name, p.data, p.trailing)
        except ProtocolError:
            pass

    def on_packet(self, name: str, d: dict[str, Any], trailing: bytes) -> None:
        auth = self.server.kind == "auth"
        if name == "HEADER_CG_HANDSHAKE":
            if d.get("dwHandshake") == HANDSHAKE:
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
            text = "Merhaba yolcu![ENTER]Ne istersin?[QUESTION 1;Görev ver|2;Hoşça kal][DONE]".encode("cp1254") + b"\0"
            self.c.send("HEADER_GC_SCRIPT", {"skin": 0, "src_size": len(text)}, text)
        elif name == "HEADER_CG_SCRIPT_ANSWER":
            self.chat(f"cevap {d['answer']}")
        elif name == "HEADER_CG_CHAT":
            msg = trailing.split(b"\0", 1)[0].decode("cp1254")
            parts = msg.split()
            if parts[:1] == ["/qa"] and self.name.startswith("AI_QA_"):
                cmd = parts[1] if len(parts) > 1 else ""
                if cmd == "gold":
                    self.me["gold"] = int(parts[2])
                    self.point(11, self.me["gold"])
                elif cmd == "hp":
                    self.me["hp"] = int(parts[2])
                    self.point(5, self.me["hp"])
                self.chat(f"[QA] OK {cmd}")
            else:
                self.chat(f"{self.name} : {msg}", ctype=0)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, kind: str, profile: Profile, world: FakeWorld):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.kind, self.profile, self.world = kind, profile, world


class FakeMetin2:
    """auth + game sunucularını thread'lerde başlatır."""

    def __init__(self, profile: Profile, password: str = "qa"):
        self.world = FakeWorld(password)
        self.auth = _Server("auth", profile, self.world)
        self.game = _Server("game", profile, self.world)
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
