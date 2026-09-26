"""QA Bridge protokolü (orchestrator <-> Metin2 QA Client).

Taşıma: TCP üzerinde satır başına bir UTF-8 JSON mesajı (JSON-lines).

İstek:   {"id": 7, "cmd": "move_to", "args": {"x": 1000, "y": 2000}}
Yanıt:   {"id": 7, "ok": true, "t": 123450, "data": {...}}
Hata:    {"id": 7, "ok": false, "t": 123450, "error": {"code": "OUT_OF_RANGE", "message": "..."}}
Olay:    {"event": "entity_dead", "t": 123460, "data": {"vid": 9212}}

`t` alanı istemcinin oyun zamanıdır (ms). Trace'ler duvar saati yerine bunu kullanır,
böylece simülatörde aynı seed ile birebir aynı trace üretilir.

Temel prensip: her aksiyon istemcideki normal oyuncu fonksiyonunu çağırır ve normal paket
olarak sunucuya gider. Protokolde veritabanına yazan bir komut YOKTUR. Test hazırlığı
(item verme vb.) sadece sunucunun QA hesaplarına açtığı `/qa ...` chat komutlarıyla yapılır.
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1

# Durum okuma komutları: oyunu değiştirmez, trace'e yazılmaz.
STATE_COMMANDS: dict[str, str] = {
    "hello": "El sıkışma: {client, protocol, capabilities}",
    "get_player_state": "Oyuncu: name, vid, level, hp, max_hp, sp, max_sp, gold, exp, x, y, map, channel, dead, in_game",
    "get_inventory": "Envanter slotları + ekipman",
    "get_nearby_entities": "Yakındaki varlıklar. args: radius?, type? (monster|npc|pc|item), vnum?",
    "get_target": "Seçili hedef",
    "get_open_windows": "Açık pencereler (dialog, shop, trade, party_invite içerikleriyle)",
    "get_quest_state": "Görev durumları",
    "get_party": "Grup: in_party, leader_vid, is_leader, members[]",
    "get_system_messages": "Sistem/chat mesajları. args: since?",
    "get_whispers": "Gelen fısıltılar. args: since? — [{seq, t, from, text, gm}]",
    "get_client_log": "İstemci log satırları. args: since?",
    "get_target_info": "args: vid — son alınan düşüş bilgisi {race, level, hp, exp, gold:[min,max], items:[{vnum,count,ppm}]} "
                       "ya da null (target_info ile istenir)",
    "get_mounts": "Binek ahırı: {active, kinds:[{kind, own, level, xp, need, bonus}]} (son MR_MOUNT_LIST)",
    "screenshot": "Ekran görüntüsü. yanıt: {format:'png', base64} veya {path}",
    "wait": "args: ms — istemci ms kadar oyun zamanı geçtikten sonra yanıt verir",
}

# Oyuncu aksiyonları: normal paket yoluyla sunucuya gider, trace'e yazılır.
ACTION_COMMANDS: dict[str, str] = {
    "login": "args: account, password",
    "select_character": "args: name? | index?",
    "logout": "",
    "move_to": "args: x, y",
    "target": "args: vid",
    "attack": "Seçili hedefe saldırı başlat",
    "stop_attack": "",
    "use_skill": "args: slot (headless: beceri vnum'u — get_player_state.skills)",
    "use_item": "args: slot",
    "equip_item": "args: slot",
    "unequip_item": "args: wear_slot",
    "drop_item": "args: slot, count?",
    "target_info": "args: vid — hedef canavarın düşüş bilgisini iste (/target_info). Headless ve sim bilgiyi "
                   "doğrudan döndürür; gerçek istemci {pending: true} döner, sonra get_target_info",
    "mount_command": "args: op (list|ride|dismount|feed), kind? (1-5) — binek ahırı (/mr2mount); yanıt get_mounts biçiminde "
                     "ya da {pending: true}",
    "split_item": "args: slot, count — yığından count kadarını boş bir slota ayır (Shift+sürükle). yanıt: {slot}",
    "pickup": "args: vid (yerdeki item)",
    "talk_to_npc": "args: vid",
    "select_dialog": "args: index",
    "close_window": "args: name",
    "buy_item": "args: slot (shop slotu)",
    "sell_item": "args: slot, count?",
    "refine_item": "args: slot, npc_vid? (demirci), scroll_slot? (yükseltme kâğıdı), confirm? — + basma. "
                   "Yanıt: {confirmed, result: success|failed, src_vnum, result_vnum, cost, prob, materials} "
                   "ya da (gerçek istemci) {pending: true}",
    "send_chat": "args: message",
    "whisper": "args: to (oyuncu adı), message — fısıltı gönder",
    "respawn": "args: here? (true: olduğun yerde)",
    "change_channel": "args: channel",
    # Ticaret (Metin2 exchange: istek iki tarafta pencereyi hemen açar)
    "trade_request": "args: vid (oyuncu)",
    "trade_add_item": "args: slot (tüm yığın eklenir)",
    "trade_set_gold": "args: amount",
    "trade_accept": "Onayla; iki taraf onaylayınca takas olur. Teklif değişirse onaylar sıfırlanır",
    "trade_cancel": "",
    # Grup
    "party_invite": "args: vid (oyuncu)",
    "party_answer": "args: accept (bool) — bekleyen daveti kabul/ret",
    "party_leave": "",
    "party_kick": "args: vid (yalnızca lider)",
}

# Yalnızca simülatörün sunduğu kontrol komutları (capability: sim_control / server_events).
SIM_COMMANDS: dict[str, str] = {
    "sim_reset": "args: seed, faults? — dünyayı deterministik başlangıç durumuna döndür",
    "qa_server_events": "args: since? — sunucu QA_EVENT/QA_ASSERT/SYSERR olayları",
}

ALL_COMMANDS = {**STATE_COMMANDS, **ACTION_COMMANDS, **SIM_COMMANDS}


class BridgeError(Exception):
    """Bağlantı/altyapı hatası (test sonucu değil, ERROR sayılır)."""


class ActionError(Exception):
    """İstemcinin reddettiği aksiyon (ok=false)."""

    def __init__(self, cmd: str, code: str, message: str, t: int | None = None):
        super().__init__(f"{cmd}: {code}: {message}")
        self.cmd = cmd
        self.code = code
        self.message = message
        self.t = t


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


def ok(req_id: int | None, t: int, data: Any = None) -> dict[str, Any]:
    return {"id": req_id, "ok": True, "t": t, "data": data if data is not None else {}}


def err(req_id: int | None, t: int, code: str, message: str) -> dict[str, Any]:
    return {"id": req_id, "ok": False, "t": t, "error": {"code": code, "message": message}}
