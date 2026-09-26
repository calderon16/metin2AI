"""Harita rehberi: oyuncunun uzaktan göremediği NPC'lerin (satıcı, demirci ...) ve canavar doğma yerlerinin
konumları, ayrıca eşya adları (model 27001'i "silah" sanmasın).

Gerçek oyuncu köyü bilir ya da haritaya bakar; model ise yalnız yakınındaki varlıkları görür. Rehber, sunucunun
harita dosyalarından (map/<ad>/npc.txt + Setting.txt BasePosition) çıkarılmış `world.json`'dan üretilir:

    python -m qa.engine.npcdir build ../build/quest-research/world.json maps/npc_directory.json

Dosya oyun verisidir; maps/ gibi git dışında tutulur. Yol QA_NPC_DIRECTORY ile değiştirilebilir.
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "maps" / "npc_directory.json"
_cache: dict[str, Any] = {}


def _hex_names(path: Path) -> dict[int, str]:
    """mob_names.tsv / item_names.tsv: vnum \t yerel_ad_hex \t ad_hex(cp1254) ..."""
    out: dict[int, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="ascii", errors="replace").splitlines():
        p = line.split("\t")
        if len(p) < 3 or not p[0].strip().isdigit():
            continue
        try:
            name = bytes.fromhex(p[2]).decode("cp1254").strip()
        except ValueError:
            continue
        if name:
            out[int(p[0])] = name
    return out


def _groups(path: Path) -> dict[int, list[int]]:
    """group.txt / group_group.txt blokları: Vnum -> üye vnum'ları (group_group'ta üyeler grup vnum'larıdır)."""
    out: dict[int, list[int]] = {}
    if not path.exists():
        return out
    vnum: int | None = None
    members: list[int] = []
    for raw in path.read_text(encoding="cp1254", errors="replace").splitlines():
        p = [x for x in raw.strip().split("\t") if x != ""]
        if not p:
            continue
        if p[0] == "Group":
            vnum, members = None, []
        elif p[0] == "Vnum" and len(p) > 1 and p[1].isdigit():
            vnum = int(p[1])
        elif p[0] == "Leader" and p[-1].isdigit():
            members.append(int(p[-1]))
        elif p[0].isdigit() and len(p) >= 2:
            # group.txt: "1  Ad  vnum" ; group_group.txt: "1  grup_vnum  ağırlık"
            nums = [int(x) for x in p[1:] if x.isdigit()]
            if nums:
                members.append(nums[-1] if len(p) >= 3 and not p[1].isdigit() else nums[0])
        elif p[0] == "}" and vnum is not None:
            out[vnum] = sorted(set(members))
    return out


def _mob_spots(qdir: Path, map_name: str, base: list[int], groups: dict[int, list[int]],
               group_groups: dict[int, list[int]], max_spots: int = 40) -> dict[int, list[list[int]]]:
    path = qdir / "map" / map_name / "regen.txt"
    spots: dict[int, list[list[int]]] = {}
    if not path.exists():
        return spots
    for raw in path.read_text(encoding="cp1254", errors="replace").splitlines():
        p = raw.strip().split("\t")
        if len(p) < 11 or p[0] not in ("m", "g", "r") or not p[10].strip().isdigit():
            continue
        try:
            x, y = base[0] + int(p[1]) * 100, base[1] + int(p[2]) * 100
        except ValueError:
            continue
        v = int(p[10])
        if p[0] == "m":
            mobs = [v]
        elif p[0] == "g":
            mobs = groups.get(v, [])
        else:
            mobs = sorted({m for g in group_groups.get(v, []) for m in groups.get(g, [])})
        for mob in mobs:
            lst = spots.setdefault(mob, [])
            # birbirine çok yakın doğma noktalarını tekrar yazma (≈20 m)
            if len(lst) < max_spots and all(abs(a - x) + abs(b - y) > 2000 for a, b in lst):
                lst.append([x, y])
    return spots


def build(world_json: Path, out: Path, qresearch: Path | None = None) -> dict[str, int]:
    world_json = Path(world_json)
    world = json.loads(world_json.read_text(encoding="utf-8"))
    qdir = Path(qresearch) if qresearch else world_json.parent / "qresearch"
    mob_names = _hex_names(qdir / "mob_names.tsv")
    groups, group_groups = _groups(qdir / "group.txt"), _groups(qdir / "group_group.txt")
    maps: dict[str, Any] = {}
    for m in world:
        npcs = [{"vnum": int(n["vnum"]), "name": n.get("name") or str(n["vnum"]), "x": int(n["x"]), "y": int(n["y"])}
                for n in m.get("npcs") or []]
        levels = {int(x["vnum"]): x.get("level") for x in m.get("mobs") or []}
        spots = _mob_spots(qdir, m["name"], m.get("base") or [0, 0], groups, group_groups)
        mobs = [{"vnum": v, "name": mob_names.get(v, str(v)), "level": levels.get(v), "spots": s}
                for v, s in sorted(spots.items())]
        if npcs or mobs:
            maps[str(m["index"])] = {"name": m.get("name"), "npcs": npcs, "mobs": mobs}
    items = {str(k): v for k, v in _hex_names(qdir / "item_names.tsv").items()}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"maps": maps, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"maps": len(maps), "npcs": sum(len(v["npcs"]) for v in maps.values()),
            "mob_kinds": sum(len(v["mobs"]) for v in maps.values()), "items": len(items)}


def path() -> Path:
    return Path(os.environ.get("QA_NPC_DIRECTORY") or DEFAULT_PATH)


def load() -> dict[str, Any] | None:
    p = path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    key = f"{p}:{mtime}"
    if key not in _cache:
        _cache.clear()
        _cache[key] = json.loads(p.read_text(encoding="utf-8"))
    return _cache[key]


def fold(text: str) -> str:
    """Büyük/küçük harf ve Türkçe karakter farkı gözetmeyen karşılaştırma anahtarı (Işınlayıcı ~ isinlayici)."""
    t = (text or "").replace("I", "ı").replace("İ", "i").lower()
    t = t.translate(str.maketrans("ıçğöşü", "icgosu"))
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)).strip()


def npcs_on_map(map_index: int | None) -> list[dict[str, Any]]:
    d = load()
    if not d or map_index is None:
        return []
    return list((d["maps"].get(str(map_index)) or {}).get("npcs") or [])


# Krallık köyleri (a = Kırmızı/Shinsoo, b = Sarı/Chunjo, c = Mavi/Jinno; 1 = ilk köy, 3 = ikinci köy)
MAP_TITLES = {
    "metin2_map_a1": "Kırmızı krallık 1. köy", "metin2_map_a3": "Kırmızı krallık 2. köy",
    "metin2_map_b1": "Sarı krallık 1. köy", "metin2_map_b3": "Sarı krallık 2. köy",
    "metin2_map_c1": "Mavi krallık 1. köy", "metin2_map_c3": "Mavi krallık 2. köy",
    "map_n_threeway": "Seungryong Vadisi", "metin2_map_n_desert_01": "Yongbi Çölü", "map_n_snowm_01": "Sohan Dağı",
    "metin2_map_n_flame_01": "Doyyumhwan", "metin2_map_milgyo": "Hwang Tapınağı", "metin2_map_trent": "Hayalet Ormanı",
    "metin2_map_trent02": "Kızıl Orman", "metin2_map_deviltower1": "Şeytan Kulesi",
    "metin2_map_spiderdungeon": "Örümcek Zindanı", "metin2_map_spiderdungeon_02": "Örümcek Zindanı 2",
}


def place(map_index: Any, x: Any, y: Any) -> str:
    """"Kırmızı krallık 1. köy (4692, 9517) — Silah Satıcısı yakını": harita adı, metre koordinat, en yakın NPC."""
    d = load() or {}
    m = (d.get("maps") or {}).get(str(map_index)) or {}
    name = MAP_TITLES.get(m.get("name", ""), m.get("name") or f"harita {map_index}")
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return name
    text = f"{name} ({int(fx) // 100}, {int(fy) // 100})"
    npcs = m.get("npcs") or []
    if npcs:
        n = min(npcs, key=lambda r: (r["x"] - fx) ** 2 + (r["y"] - fy) ** 2)
        dist = ((n["x"] - fx) ** 2 + (n["y"] - fy) ** 2) ** 0.5
        if dist <= 3000:
            text += f" — {n['name']} yakını"
    return text


def item_name(vnum: Any) -> str:
    d = load()
    return str(((d or {}).get("items") or {}).get(str(vnum)) or vnum)


def mobs_on_map(map_index: int | None) -> list[dict[str, Any]]:
    d = load()
    if not d or map_index is None:
        return []
    return list((d["maps"].get(str(map_index)) or {}).get("mobs") or [])


def find_mob(map_index: int | None, name: str | None = None, vnum: int | None = None) -> list[dict[str, Any]]:
    rows = mobs_on_map(map_index)
    if vnum is not None:
        return [r for r in rows if r["vnum"] == int(vnum)]
    q = fold(name or "")
    if not q:
        return []
    exact = [r for r in rows if fold(r["name"]) == q]
    return exact or [r for r in rows if q in fold(r["name"])]


def find(map_index: int | None, name: str | None = None, vnum: int | None = None) -> list[dict[str, Any]]:
    rows = npcs_on_map(map_index)
    if vnum is not None:
        return [r for r in rows if r["vnum"] == int(vnum)]
    q = fold(name or "")
    if not q:
        return []
    exact = [r for r in rows if fold(r["name"]) == q]
    return exact or [r for r in rows if q in fold(r["name"])]


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) != 4 or sys.argv[1] != "build":
        print(__doc__)
        sys.exit(2)
    print(json.dumps(build(Path(sys.argv[2]), Path(sys.argv[3]))))
