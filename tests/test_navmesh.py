"""Yol bulma: LZO1X çözücü (sunucunun server_attr sektörü), A* engellerden dolaşma ve köşe kesmeme."""

import hashlib
from pathlib import Path

import pytest

from qa.headless.navmesh import NavError, NavGrid, lzo1x_decompress

D = Path(__file__).parent / "data"


def test_lzo_sector_from_real_server_attr():
    # metin2_map_a1'den gerçek bir sektör: hem engelli hem açık hücre içerir
    out = lzo1x_decompress((D / "lzo_sectree.bin").read_bytes(), 70000)
    assert len(out) == 128 * 128 * 4
    assert hashlib.sha256(out).hexdigest() == "4d93612318c833900e9d7863753234064cbc792e6a37527fd70170ecde769616"


def _grid(rows: list[str]) -> NavGrid:
    """'.' yürünebilir, '#' engel; her karakter bir düğüm (100 birim)."""
    h, w = len(rows), len(rows[0])
    walk = bytearray(1 if c == "." else 0 for r in rows for c in r)
    return NavGrid(0, 0, w, h, walk)


def test_path_goes_around_a_wall_and_never_through_it():
    g = _grid(["..........",
               "....#.....",
               "....#.....",
               "....#.....",
               ".........."])
    start, goal = g.to_world(1, 2), g.to_world(8, 2)
    assert not g.line_free(g.to_node(*start), g.to_node(*goal))
    path = g.find_path(start, goal)
    pts = [g.to_node(*start)] + [g.to_node(*p) for p in path]
    for a, b in zip(pts, pts[1:]):
        assert g.line_free(a, b)            # her parça yürünebilir
    assert pts[-1] == g.to_node(*goal)


def test_no_corner_cutting_between_diagonal_blocks():
    g = _grid(["..#",
               "#..",
               "..."])
    assert not g.line_free((1, 0), (0, 1)) and not g.line_free((0, 0), (1, 1))


def test_blocked_goal_snaps_to_nearest_and_unreachable_raises():
    g = _grid(["...#...",
               "...#...",
               "...#..."])
    with pytest.raises(NavError, match="ulaşılamıyor"):
        g.find_path(g.to_world(0, 1), g.to_world(6, 1))
    g2 = _grid([".....",
                "..#..",
                "....."])
    path = g2.find_path(g2.to_world(0, 1), g2.to_world(2, 1))   # hedef engel: yanına gidilir
    assert g2.walkable_world(*path[-1])


def test_grid_save_load_roundtrip(tmp_path):
    g = _grid(["..#.", "...."])
    g.save(tmp_path / "nav.bin")
    g2 = NavGrid.load(tmp_path / "nav.bin")
    assert (g2.w, g2.h, bytes(g2.walk)) == (g.w, g.h, bytes(g.walk))
