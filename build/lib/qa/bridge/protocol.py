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
    "get_client_log": "İstemci log satırları. args: since?",
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
    "use_skill": "args: slot",
    "use_item": "args: slot",
    "equip_item": "args: slot",
    "unequip_item": "args: wear_slot",
    "drop_item": "args: slot, count?",
    "pickup": "args: vid (yerdeki item)",
    "talk_to_npc": "args: vid",
    "select_dialog": "args: index",
    "close_window": "args: name",
    "buy_item": "args: slot (shop slotu)",
    "sell_item": "args: slot, count?",
    "send_chat": "args: message",
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
