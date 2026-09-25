"""Bağlamalar (bindings): mantıksal mesaj → fork'taki header ve alan adları.

HeadlessClient "karakter seç", "hareket et" gibi mantıksal işler yapar; bunun hangi header ve hangi
struct alanlarıyla yapılacağı fork'a göre değişir. Varsayılanlar klasik (2014 sonrası yaygın) kaynak
adlarıdır. Her mesajda birden çok aday header / alan adı verilebilir; profilde ilk bulunan kullanılır.

Fork farklıysa profille aynı yere `<profil>.bindings.yaml` koyun; yalnızca değişen anahtarları yazın:

    packets:
      login_success: {gc: [HEADER_GC_LOGIN_SUCCESS_NEW], players: players}
    points: {GOLD: 12}
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from .profile import Profile, ProfileError

DEFAULT_BINDINGS: dict[str, Any] = {
    # EPoints (common/length.h) — klasik sıra
    "points": {"LEVEL": 1, "EXP": 3, "HP": 5, "MAX_HP": 6, "SP": 7, "MAX_SP": 8, "GOLD": 11},
    # CInstanceBase / CHARACTER::FUNC_*
    "move_funcs": {"WAIT": 0, "MOVE": 1, "ATTACK": 3},
    # ECharType: bType → protokol türü
    "char_types": {0: "monster", 1: "npc", 2: "monster", 3: "warp", 4: "other", 5: "other", 6: "pc", 7: "pc",
                   8: "other", 9: "other"},
    "chat_types": {"TALKING": 0, "INFO": 1, "NOTICE": 2, "PARTY": 3, "GUILD": 4, "COMMAND": 5, "SHOUT": 6,
                   "WHISPER": 7},
    "inventory_window": 1,
    "inventory_size": 90,          # INVENTORY_MAX_NUM; üstündeki hücreler ekipmandır
    "wear_names": {0: "body", 1: "head", 2: "shoes", 3: "wrist", 4: "weapon", 5: "neck", 6: "ear",
                   7: "unique1", 8: "unique2", 9: "arrow", 10: "shield"},
    "attack_range": 300,
    "interact_range": 400,
    "attack_interval_ms": 700,
    "script_close_answer": 254,
    "phases": {"HANDSHAKE": "PHASE_HANDSHAKE", "LOGIN": "PHASE_LOGIN", "SELECT": "PHASE_SELECT",
               "LOADING": "PHASE_LOADING", "GAME": "PHASE_GAME", "AUTH": "PHASE_AUTH", "DEAD": "PHASE_DEAD"},
    "packets": {
        "handshake": {"gc": ["HEADER_GC_HANDSHAKE"], "cg": ["HEADER_CG_HANDSHAKE"]},
        "phase": {"gc": ["HEADER_GC_PHASE"], "phase": ["phase"]},
        "ping": {"gc": ["HEADER_GC_PING"], "cg": ["HEADER_CG_PONG"]},
        "auth_login": {"cg": ["HEADER_CG_LOGIN3", "HEADER_CG_LOGIN5_OPENID"], "login": ["login", "szLogin"],
                       "password": ["passwd", "szPasswd"]},
        "auth_success": {"gc": ["HEADER_GC_AUTH_SUCCESS"], "key": ["dwLoginKey"], "result": ["bResult"]},
        "login_failure": {"gc": ["HEADER_GC_LOGIN_FAILURE"], "status": ["szStatus"]},
        "game_login": {"cg": ["HEADER_CG_LOGIN2"], "login": ["login", "szLogin"], "key": ["dwLoginKey"]},
        "login_success": {"gc": ["HEADER_GC_LOGIN_SUCCESS4", "HEADER_GC_LOGIN_SUCCESS3"], "players": ["players"],
                          "name": ["szName"], "level": ["byLevel"], "id": ["dwID"]},
        "select": {"cg": ["HEADER_CG_CHARACTER_SELECT", "HEADER_CG_PLAYER_SELECT"], "index": ["index"]},
        "enter_game": {"cg": ["HEADER_CG_ENTERGAME"]},
        "main_character": {"gc": ["HEADER_GC_MAIN_CHARACTER", "HEADER_GC_MAIN_CHARACTER2_EMPIRE",
                                  "HEADER_GC_MAIN_CHARACTER3_BGM", "HEADER_GC_MAIN_CHARACTER4_BGM_VOL"],
                           "vid": ["dwVID"], "name": ["szName", "szChrName"], "x": ["lx", "lX"], "y": ["ly", "lY"]},
        "points": {"gc": ["HEADER_GC_CHARACTER_POINTS"], "array": ["points"]},
        "point_change": {"gc": ["HEADER_GC_CHARACTER_POINT_CHANGE"], "vid": ["dwVID"], "type": ["type", "Type"],
                         "value": ["value", "lValue"]},
        "char_add": {"gc": ["HEADER_GC_CHARACTER_ADD", "HEADER_GC_CHARACTER_ADD2"], "vid": ["dwVID"],
                     "x": ["x", "lX"], "y": ["y", "lY"], "type": ["bType"], "race": ["wRaceNum"]},
        "char_info": {"gc": ["HEADER_GC_CHAR_ADDITIONAL_INFO"], "vid": ["dwVID"], "name": ["name", "szName"],
                      "level": ["dwLevel"]},
        "char_del": {"gc": ["HEADER_GC_CHARACTER_DEL"], "vid": ["id", "dwVID"]},
        "char_move": {"gc": ["HEADER_GC_MOVE"], "vid": ["dwVID"], "x": ["lX"], "y": ["lY"], "func": ["bFunc"]},
        "dead": {"gc": ["HEADER_GC_DEAD"], "vid": ["vid", "dwVID"]},
        "item_set": {"gc": ["HEADER_GC_ITEM_SET", "HEADER_GC_ITEM_SET2"], "pos": ["Cell", "pos"],
                     "vnum": ["vnum", "dwVnum"], "count": ["count", "bCount"]},
        "item_ground_add": {"gc": ["HEADER_GC_ITEM_GROUND_ADD"], "vid": ["dwVID"], "vnum": ["dwVnum"],
                            "x": ["x", "lX"], "y": ["y", "lY"]},
        "item_ground_del": {"gc": ["HEADER_GC_ITEM_GROUND_DEL"], "vid": ["vid", "dwVID"]},
        "chat_gc": {"gc": ["HEADER_GC_CHAT"], "type": ["type"], "vid": ["id", "dwVID"]},
        "script": {"gc": ["HEADER_GC_SCRIPT"]},
        "move": {"cg": ["HEADER_CG_MOVE", "HEADER_CG_CHARACTER_MOVE"], "func": ["bFunc"], "arg": ["bArg"],
                 "rot": ["bRot"], "x": ["lX"], "y": ["lY"], "time": ["dwTime"]},
        "chat": {"cg": ["HEADER_CG_CHAT"], "type": ["type"]},
        "attack": {"cg": ["HEADER_CG_ATTACK"], "type": ["bType"], "vid": ["dwVID", "dwVictimVID"]},
        "target": {"cg": ["HEADER_CG_TARGET"], "vid": ["dwVID"]},
        "item_use": {"cg": ["HEADER_CG_ITEM_USE"], "pos": ["Cell", "pos"]},
        "item_pickup": {"cg": ["HEADER_CG_ITEM_PICKUP"], "vid": ["vid", "dwVID"]},
        "on_click": {"cg": ["HEADER_CG_ON_CLICK"], "vid": ["vid", "dwVID"]},
        "script_answer": {"cg": ["HEADER_CG_SCRIPT_ANSWER"], "answer": ["answer"]},
    },
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Bindings:
    def __init__(self, profile: Profile, overrides: dict[str, Any] | None = None):
        self.profile = profile
        self.b = _merge(DEFAULT_BINDINGS, overrides or {})
        self.gc_names: dict[str, str] = {}          # header adı → mantıksal ad
        for logical, spec in self.b["packets"].items():
            for h in _aslist(spec.get("gc")):
                if h in profile.packets:
                    self.gc_names.setdefault(h, logical)

    @classmethod
    def for_profile(cls, profile: Profile, profile_path: Path | None) -> "Bindings":
        over: dict[str, Any] = {}
        if profile_path is not None:
            p = profile_path.with_name(profile_path.stem + ".bindings.yaml")
            if p.exists():
                over = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(profile, over)

    def __getitem__(self, k: str) -> Any:
        return self.b[k]

    def spec(self, logical: str) -> dict[str, Any]:
        return self.b["packets"][logical]

    def cg(self, logical: str) -> str:
        for h in _aslist(self.spec(logical).get("cg")):
            if h in self.profile.packets and self.profile.packets[h].get("struct"):
                return h
        raise ProfileError(f"'{logical}' için profilde CG paketi yok (adaylar: {self.spec(logical).get('cg')})")

    def supports(self, logical: str) -> bool:
        try:
            self.cg(logical)
            return True
        except (ProfileError, KeyError):
            return False

    def field(self, logical: str, key: str, header_name: str) -> str:
        """Struct'ta bulunan ilk aday alan adı."""
        st = self.profile.structs[self.profile.resolve(self.profile.struct_for(header_name))]
        names = {f.name for f in st.fields}
        for cand in _aslist(self.spec(logical).get(key)):
            if cand in names:
                return cand
        raise ProfileError(f"{header_name}: '{key}' alanı bulunamadı (adaylar: {self.spec(logical).get(key)})")

    def get(self, logical: str, key: str, pkt_name: str, data: dict[str, Any], default: Any = None) -> Any:
        try:
            return data.get(self.field(logical, key, pkt_name), default)
        except ProfileError:
            return default

    def values(self, logical: str, header_name: str, **kv: Any) -> dict[str, Any]:
        """Mantıksal anahtarları struct alan adlarına çevir."""
        return {self.field(logical, k, header_name): v for k, v in kv.items()}

    def phase(self, key: str) -> int | None:
        return self.profile.constants.get(self.b["phases"].get(key, ""))


def _aslist(v: Any) -> list[str]:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]
