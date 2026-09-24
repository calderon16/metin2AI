"""Simülatör için bağımlılıksız PNG "ekran görüntüsü": oyuncu çevresinin kuşbakışı haritası."""

from __future__ import annotations

import struct
import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .world import Player, SimWorld

SIZE = 256
VIEW = 6000  # oyuncu etrafında gösterilen alan (birim)

COLORS = {
    "bg": (34, 60, 34),
    "pc": (255, 255, 255),
    "other_pc": (180, 180, 255),
    "monster": (220, 50, 50),
    "npc": (60, 120, 255),
    "item": (255, 220, 0),
}


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(pixels: list[bytearray]) -> bytes:
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b"")


def render_png(world: "SimWorld", player: "Player") -> bytes:
    pixels = [bytearray(COLORS["bg"] * SIZE) for _ in range(SIZE)]

    def dot(x: float, y: float, color: tuple[int, int, int], r: int) -> None:
        cx = int((x - player.x) / VIEW * SIZE + SIZE / 2)
        cy = int((y - player.y) / VIEW * SIZE + SIZE / 2)
        for yy in range(cy - r, cy + r + 1):
            for xx in range(cx - r, cx + r + 1):
                if 0 <= xx < SIZE and 0 <= yy < SIZE:
                    pixels[yy][xx * 3:xx * 3 + 3] = bytes(color)

    for e in world.entities.values():
        if e.type == "monster" and e.dead:
            continue
        dot(e.x, e.y, COLORS[e.type], 2 if e.type == "item" else 3)
    for o in world.players.values():
        if o is not player and o.in_game and o.map == player.map:
            dot(o.x, o.y, COLORS["other_pc"], 3)
    dot(player.x, player.y, COLORS["pc"], 4)
    return encode_png(pixels)
