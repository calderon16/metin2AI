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
SHOP_VID, SMITH_VID = 320, 330   # oyuncu paketleri olan profilde: silah satıcısı ve demirci
SHOP_ITEMS = [(10, 100), (27001, 50)]   # (vnum, fiyat)
MAX_STEP = 400  # tek MOVE paketinde izin verilen en büyük mesafe (hız kontrolü)


class FakeWorld:
    def __init__(self, password: str = "qa"):
        self.password = password
        self.lock = threading.Lock()
        self.whispers_to_owner: list[str] = []
        self.owner_offer_gold = 0     # >0: TESTR, ajanın açtığı ticarete bu kadar yang koyup onaylar
        self.handlers: list[Any] = []
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
        with self.w.lock:
            self.w.handlers.append(self)
        self.quest_step = ""
        self.pending = None
        self.trade = None
        self.key_agreement: ImprovedKeyAgreement | None = None
        self.player_packets = "HEADER_GC_SHOP" in self.server.profile.packets
        if self.player_packets:
            # input_main.cpp Shop: BUY → [adet, sıra], SELL → [hücre], SELL2 → [hücre, adet]
            self.c.extra_sized[self.server.profile.header("HEADER_CG_SHOP")] =                 lambda buf: {1: 2, 2: 1, 3: 2}.get(buf[1], 0)
        self.refining: tuple[int, int] | None = None

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
        self.hs_time = int(time.time() * 1000) & 0xFFFFFFFF
        self.send("HEADER_GC_HANDSHAKE", dwHandshake=HANDSHAKE, dwTime=self.hs_time, lDelta=0)
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
            if self.player_packets:
                self.me["items"].update({2: [10, 1], 90: [11200, 1]})   # envanterde kılıç, sırtta zırh
                self.item(2, 10, 1)
                self.item(90, 11200, 1)
        elif name == "HEADER_CG_ENTERGAME":
            self.phase("PHASE_GAME")
            self.send("HEADER_GC_CHARACTER_ADD", dwVID=MOB_VID, x=5200, y=5000, bType=0, wRaceNum=101)
            self.send("HEADER_GC_CHAR_ADDITIONAL_INFO", dwVID=MOB_VID, name="Yaban Köpeği")
            self.send("HEADER_GC_CHARACTER_ADD", dwVID=NPC_VID, x=4800, y=5000, bType=1, wRaceNum=9001)
            self.send("HEADER_GC_CHAR_ADDITIONAL_INFO", dwVID=NPC_VID, name="Satıcı")
            if "HEADER_GC_EXCHANGE" in self.server.profile.packets:
                self.send("HEADER_GC_CHARACTER_ADD", dwVID=PC_VID, x=5100, y=5000, bType=6, wRaceNum=0)
                self.send("HEADER_GC_CHAR_ADDITIONAL_INFO", dwVID=PC_VID, name="TESTR")
            if self.player_packets:
                self.send("HEADER_GC_CHARACTER_ADD", dwVID=SHOP_VID, x=4900, y=4900, bType=1, wRaceNum=9002)
                self.send("HEADER_GC_CHAR_ADDITIONAL_INFO", dwVID=SHOP_VID, name="Silah Satıcısı")
                self.send("HEADER_GC_CHARACTER_ADD", dwVID=SMITH_VID, x=4900, y=5100, bType=1, wRaceNum=20016)
                self.send("HEADER_GC_CHAR_ADDITIONAL_INFO", dwVID=SMITH_VID, name="Demirci")
                skills = [{"bMasterType": 0, "bLevel": 0, "tNextRead": 0} for _ in range(255)]
                skills[3]["bLevel"] = 5
                self.send("HEADER_GC_SKILL_LEVEL_NEW", skills=skills)
            self.send("HEADER_GC_PING")
        elif name == "HEADER_CG_PONG":
            with self.w.lock:
                self.w.stats["pongs"] += 1
        elif name == "HEADER_CG_MOVE":
            step = ((d["lX"] - self.me["x"]) ** 2 + (d["lY"] - self.me["y"]) ** 2) ** 0.5
            with self.w.lock:
                self.w.stats["moves"] += 1
                # gerçek sunucu: dwCurTime - dwTime >= 30000 → "SPEEDHACK: slow timer"
                skew = abs((int(time.time() * 1000) & 0xFFFFFFFF) - int(d.get("dwTime", 0))) if "dwTime" in d else 0
                self.w.stats["max_time_skew_ms"] = max(self.w.stats.get("max_time_skew_ms", 0), skew)
                if step > MAX_STEP:
                    self.w.stats["speed_violations"] += 1
            self.me["x"], self.me["y"] = d["lX"], d["lY"]
        elif name == "HEADER_CG_ATTACK":
            with self.w.lock:
                self.w.stats["attacks"] += 1
                if d.get("bType"):
                    self.w.stats["skill_hits"] = self.w.stats.get("skill_hits", 0) + 1
            if d["dwVID"] == MOB_VID and self.hp > 0:
                self.hp -= 1
                if self.hp == 0:
                    self.send("HEADER_GC_DEAD", vid=MOB_VID)
                    self.send("HEADER_GC_ITEM_GROUND_ADD", x=5210, y=5000, z=0, dwVID=400, dwVnum=30000)
                    self.point(3, 20)
        elif name == "HEADER_CG_ITEM_PICKUP" and d["vid"] == 400:
            self.send("HEADER_GC_ITEM_GROUND_DEL", vid=400)
            self.me["items"][1] = [30000, 1]
            self.item(1, 30000, 1)
        elif name == "HEADER_CG_ITEM_USE" and d["Cell"]["cell"] >= 90 and d["Cell"]["cell"] in self.me["items"]:
            # giyili eşyaya sağ tık: çıkar (ilk boş envanter hücresine)
            cell = d["Cell"]["cell"]
            free = next(i for i in range(90) if i not in self.me["items"])
            self.me["items"][free] = self.me["items"].pop(cell)
            self.item(cell, 0, 0)
            self.item(free, self.me["items"][free][0], 1)
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
        elif name == "HEADER_CG_ON_CLICK" and d["vid"] == SHOP_VID:
            self.open_shop()
        elif name == "HEADER_CG_SHOP":
            self.on_shop(d["subheader"], trailing)
        elif name == "HEADER_CG_GIVE_ITEM":
            self.on_give_item(d)
        elif name == "HEADER_CG_ITEM_USE_TO_ITEM":
            src = self.me["items"].get(d["source_pos"]["cell"], [0, 0])
            if src[0] == 25040:
                self.refine_info(d["target_pos"]["cell"], 2)
        elif name == "HEADER_CG_REFINE":
            self.on_refine(d)
        elif name == "HEADER_CG_USE_SKILL":
            with self.w.lock:
                self.w.stats.setdefault("skills_used", []).append((d["dwVnum"], d["dwTargetVID"]))
        elif name == "HEADER_CG_ITEM_MOVE":
            src, dst, n = d["pos"]["cell"], d["change_pos"]["cell"], d["num"]
            vnum, have = self.me["items"].get(src, [0, 0])
            if vnum and 0 < n < have and dst not in self.me["items"]:
                self.me["items"][src] = [vnum, have - n]
                self.me["items"][dst] = [vnum, n]
                self.item(src, vnum, have - n)
                self.item(dst, vnum, n)
        elif name == "HEADER_CG_ITEM_DROP2":
            cell = d["pos"]["cell"]
            vnum, count = self.me["items"].get(cell, [0, 0])
            if vnum:
                left = count - d["count"]
                if left > 0:
                    self.me["items"][cell] = [vnum, left]
                else:
                    self.me["items"].pop(cell)
                self.item(cell, vnum if left > 0 else 0, max(left, 0))
                self.send("HEADER_GC_ITEM_GROUND_ADD", x=self.me["x"], y=self.me["y"], z=0, dwVID=410, dwVnum=vnum)
        elif name == "HEADER_CG_EXCHANGE":
            self.on_exchange(d)
        elif name == "HEADER_CG_WHISPER":
            to = d.get("szNameTo", "")
            if to.lower() == "testr":
                with self.w.lock:
                    self.w.whispers_to_owner.append(trailing.split(b"\0", 1)[0].decode("cp1254"))
            else:
                self.c.send("HEADER_GC_WHISPER", {"bType": 1, "szNameFrom": to}, b"\0")
        elif name == "HEADER_CG_SCRIPT_ANSWER":
            self.chat(f"cevap {d['answer']}")
            self.on_quest_answer(d["answer"])
        elif name == "HEADER_CG_CHAT":
            msg = trailing.split(b"\0", 1)[0].decode("cp1254")
            parts = msg.split()
            if parts[:1] == ["/qa"] and self.name.startswith("AI_QA_"):
                cmd = parts[1] if len(parts) > 1 else ""
                with self.w.lock:
                    self.w.stats.setdefault("qa_commands", []).append(msg)
                if cmd == "gold":
                    self.me["gold"] = int(parts[2])
                    self.point(11, self.me["gold"])
                elif cmd == "owner_says":
                    # TESTR (GM) ajana fısıldar
                    raw = " ".join(parts[2:]).encode("cp1254") + b"\0"
                    self.c.send("HEADER_GC_WHISPER", {"bType": 5, "szNameFrom": "TESTR"}, raw)
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
            self.trade = {"items": {}, "gold": 0, "their_gold": 0}
            ex(0, 1, PC_VID)
            offer = self.w.owner_offer_gold
            if offer:
                # TESTR (sahip) yang koyup önce kendisi onaylar; ajan onaylayınca takas olur
                self.trade["their_gold"] = offer
                ex(3, 0, offer)
                ex(4, 0, 1)
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
            self.me["gold"] += self.trade.get("their_gold", 0) - self.trade["gold"]
            self.point(11, self.me["gold"])
            with self.w.lock:
                self.w.stats["trades"] = self.w.stats.get("trades", 0) + 1
            self.trade = None
            ex(5, 0)
        elif sub == 5:
            self.trade = None
            ex(5, 0)

    def shop_packet(self, sub: int, raw: bytes = b"") -> None:
        self.c.send("HEADER_GC_SHOP", {"subheader": sub}, raw)

    def open_shop(self) -> None:
        prof = self.server.profile
        items = [prof.encode("packet_shop_item", {"vnum": v, "price": p, "count": 1, "display_pos": i})
                 for i, (v, p) in enumerate(SHOP_ITEMS)]
        empty = prof.encode("packet_shop_item", {})
        raw = SHOP_VID.to_bytes(4, "little") + b"".join(items) + empty * (40 - len(items))
        self.shop_packet(0, raw)

    def free_cell(self) -> int | None:
        return next((i for i in range(90) if i not in self.me["items"]), None)

    def on_shop(self, sub: int, raw: bytes) -> None:
        if sub == 0:
            self.shop_packet(1)
        elif sub == 1:
            pos = raw[1]
            if pos >= len(SHOP_ITEMS):
                return self.shop_packet(8)
            vnum, price = SHOP_ITEMS[pos]
            if self.me["gold"] < price:
                return self.shop_packet(5)
            cell = self.free_cell()
            if cell is None:
                return self.shop_packet(7)
            self.me["gold"] -= price
            self.point(11, self.me["gold"])
            self.me["items"][cell] = [vnum, 1]
            self.item(cell, vnum, 1)      # başarıda GC_SHOP yanıtı yok (gerçek sunucu gibi)
        elif sub == 3:
            cell, count = raw[0], raw[1]
            vnum, have = self.me["items"].get(cell, [0, 0])
            if vnum and 0 < count <= have:
                left = have - count
                if left:
                    self.me["items"][cell] = [vnum, left]
                else:
                    self.me["items"].pop(cell)
                self.item(cell, vnum if left else 0, left)
                self.me["gold"] += 20 * count
                self.point(11, self.me["gold"])

    def refine_info(self, cell: int, rtype: int) -> None:
        vnum = self.me["items"].get(cell, [0, 0])[0]
        if vnum != 10:
            return self.chat("Bu eşya yükseltilemez.")
        self.refining = (cell, rtype)
        mats = [{"vnum": 30000, "count": 1}] + [{}] * 4
        self.send("HEADER_GC_REFINE_INFORMATION_NEW", type=rtype, pos=cell,
                  refine_table={"src_vnum": 10, "result_vnum": 11, "material_count": 1, "cost": 300, "prob": 100,
                                "materials": mats})

    def on_give_item(self, d: dict) -> None:
        if d["dwTargetVID"] == SMITH_VID:
            self.refine_info(d["ItemPos"]["cell"], 0)

    def on_refine(self, d: dict) -> None:
        r, self.refining = self.refining, None
        if d["type"] == 255 or r is None or r[0] != d["pos"]:
            return
        mat = next((c for c, (v, _) in self.me["items"].items() if v == 30000), None)
        if self.me["gold"] < 300 or mat is None:
            return self.chat("Yükseltme için gerekenler eksik.")
        self.me["gold"] -= 300
        self.point(11, self.me["gold"])
        self.me["items"].pop(mat)
        self.item(mat, 0, 0)
        self.me["items"][d["pos"]] = [11, 1]
        self.item(d["pos"], 11, 1)
        self.chat("RefineSuceeded", ctype=5)

    for fn in (script, mrq, on_quest_answer, on_exchange, shop_packet, open_shop, free_cell, on_shop, refine_info,
               on_give_item, on_refine):
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

    def owner_whisper(self, text: str, sender: str = "TESTR") -> int:
        """Oyundaki (oyun sunucusuna bağlı) herkese sahibin fısıltısı; ulaşılan oyuncu sayısı."""
        n = 0
        for h in list(self.world.handlers):
            if h.server.kind == "game" and h.name:
                try:
                    h.c.send("HEADER_GC_WHISPER", {"bType": 5, "szNameFrom": sender}, text.encode("cp1254") + b"\0")
                    n += 1
                except Exception:  # noqa: BLE001 — kapanmış bağlantı
                    pass
        return n

    def shutdown(self) -> None:
        for s in (self.auth, self.game):
            s.shutdown()
            s.server_close()
