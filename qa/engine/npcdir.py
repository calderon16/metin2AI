"""Harita NPC rehberi: oyuncunun uzaktan göremediği NPC'lerin (satıcı, demirci ...) ad ve konumları.

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


def build(world_json: Path, out: Path) -> dict[str, int]:
    world = json.loads(Path(world_json).read_text(encoding="utf-8"))
    maps: dict[str, Any] = {}
    for m in world:
        npcs = [{"vnum": int(n["vnum"]), "name": n.get("name") or str(n["vnum"]), "x": int(n["x"]), "y": int(n["y"])}
                for n in m.get("npcs") or []]
        if npcs:
            maps[str(m["index"])] = {"name": m.get("name"), "npcs": npcs}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"maps": maps}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"maps": len(maps), "npcs": sum(len(v["npcs"]) for v in maps.values())}


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
