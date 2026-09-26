"""Sunucunun yürünebilirlik verisiyle (map/<ad>/server_attr) yol bulma — AI oyuncu duvardan geçmez.

server_attr biçimi (game/src/sectree_manager.cpp LoadAttribute): int32 genişlik, int32 yükseklik (sektör);
her sektör (y dış, x iç döngü) için uint32 boyut + LZO1X ile sıkıştırılmış 128×128 DWORD öznitelik
(hücre = 50 birim, sektör = 6400 birim). Sunucu bir konumu `IsMovablePosition` ile ATTR_BLOCK | ATTR_OBJECT
bitlerine bakarak reddeder; burada da aynı kural kullanılır. Arama 100 birimlik (1 m) ızgarada yapılır:
2×2 hücrenin hepsi yürünebilir olmalı (dar geçitlerde bile güvenli).
"""

from __future__ import annotations

import heapq
import math
import struct
import zlib
from pathlib import Path

SECTREE_SIZE = 6400
CELL_SIZE = 50
CELLS = SECTREE_SIZE // CELL_SIZE          # 128
ATTR_BLOCK = 1 << 0
ATTR_WATER = 1 << 1
ATTR_OBJECT = 1 << 7
BLOCKING = ATTR_BLOCK | ATTR_OBJECT
NODE = 2                                    # arama düğümü = 2×2 hücre (100 birim)


class NavError(Exception):
    pass


def lzo1x_decompress(src: bytes, max_out: int) -> bytes:
    """LZO1X (minilzo lzo1x_decompress) saf Python. Crypto++'ta olduğu gibi sunucu da bu biçimi kullanır."""
    out = bytearray()
    ip, n = 0, len(src)

    def copy_match(m_pos: int, length: int) -> None:
        if m_pos < 0:
            raise NavError("LZO: geçersiz geri başvuru")
        if length <= len(out) - m_pos:
            out.extend(out[m_pos:m_pos + length])
        else:
            for i in range(length):
                out.append(out[m_pos + i])

    state = 0
    t = 0
    if src[0] > 17:
        t = src[0] - 17
        ip = 1
        out.extend(src[ip:ip + t])
        ip += t
        state = 1                            # first_literal_run
    while True:
        if state == 0:
            t = src[ip]
            ip += 1
            if t >= 16:
                state = 2
                continue
            if t == 0:
                while src[ip] == 0:
                    t += 255
                    ip += 1
                t += 15 + src[ip]
                ip += 1
            out.extend(src[ip:ip + t + 3])
            ip += t + 3
            state = 1
            continue
        if state == 1:                       # first_literal_run
            t = src[ip]
            ip += 1
            if t >= 16:
                state = 2
                continue
            m_pos = len(out) - (1 + 0x0800) - (t >> 2) - (src[ip] << 2)
            ip += 1
            copy_match(m_pos, 3)
            state = 3
            continue
        if state == 2:                       # match
            if t >= 64:
                m_pos = len(out) - 1 - ((t >> 2) & 7) - (src[ip] << 3)
                ip += 1
                copy_match(m_pos, (t >> 5) - 1 + 2)
            elif t >= 32:
                t &= 31
                if t == 0:
                    while src[ip] == 0:
                        t += 255
                        ip += 1
                    t += 31 + src[ip]
                    ip += 1
                m_pos = len(out) - 1 - ((src[ip] >> 2) + (src[ip + 1] << 6))
                ip += 2
                copy_match(m_pos, t + 2)
            elif t >= 16:
                m_pos = len(out) - ((t & 8) << 11)
                t &= 7
                if t == 0:
                    while src[ip] == 0:
                        t += 255
                        ip += 1
                    t += 7 + src[ip]
                    ip += 1
                m_pos -= (src[ip] >> 2) + (src[ip + 1] << 6)
                ip += 2
                if m_pos == len(out):
                    break                    # akış sonu
                copy_match(m_pos - 0x4000, t + 2)
            else:                            # match_next'ten gelen 2 baytlık M1
                m_pos = len(out) - 1 - (t >> 2) - (src[ip] << 2)
                ip += 1
                copy_match(m_pos, 2)
            state = 3
            continue
        if state == 3:                       # match_done
            t = src[ip - 2] & 3
            if t == 0:
                state = 0
                continue
            out.extend(src[ip:ip + t])       # match_next
            ip += t
            t = src[ip]
            ip += 1
            state = 2
            continue
        if len(out) > max_out or ip > n:
            raise NavError("LZO: taşma")
    return bytes(out)


class NavGrid:
    """Harita başına yürünebilirlik ızgarası (1 bayt/düğüm, 1 = yürünebilir)."""

    def __init__(self, base_x: int, base_y: int, width_nodes: int, height_nodes: int, walk: bytearray):
        self.base_x, self.base_y = base_x, base_y
        self.w, self.h = width_nodes, height_nodes
        self.walk = walk

    # ------------------------------------------------------------------ yükleme
    @classmethod
    def from_server_attr(cls, path: Path, base_x: int, base_y: int) -> "NavGrid":
        data = Path(path).read_bytes()
        sw, sh = struct.unpack_from("<ii", data, 0)
        off = 8
        cw, ch = sw * CELLS, sh * CELLS
        cells = bytearray(cw * ch)           # 1 = engelli
        want = CELLS * CELLS * 4
        for sy in range(sh):
            for sx in range(sw):
                size = struct.unpack_from("<I", data, off)[0]
                off += 4
                raw = lzo1x_decompress(data[off:off + size], want + 64)
                off += size
                if len(raw) != want:
                    raise NavError(f"{path}: sektör ({sx},{sy}) {len(raw)} bayt (beklenen {want})")
                attrs = struct.unpack(f"<{CELLS * CELLS}I", raw)
                row0 = sy * CELLS
                col0 = sx * CELLS
                for cy in range(CELLS):
                    line = attrs[cy * CELLS:(cy + 1) * CELLS]
                    base = (row0 + cy) * cw + col0
                    cells[base:base + CELLS] = bytes(1 if a & BLOCKING else 0 for a in line)
        nw, nh = cw // NODE, ch // NODE
        walk = bytearray(nw * nh)
        for ny in range(nh):
            r0 = (ny * NODE) * cw
            r1 = r0 + cw
            for nx in range(nw):
                c = nx * NODE
                if not (cells[r0 + c] or cells[r0 + c + 1] or cells[r1 + c] or cells[r1 + c + 1]):
                    walk[ny * nw + nx] = 1
        return cls(base_x, base_y, nw, nh, walk)

    def save(self, path: Path) -> None:
        head = struct.pack("<iiii", self.base_x, self.base_y, self.w, self.h)
        Path(path).write_bytes(head + zlib.compress(bytes(self.walk), 6))

    @classmethod
    def load(cls, path: Path) -> "NavGrid":
        data = Path(path).read_bytes()
        bx, by, w, h = struct.unpack_from("<iiii", data, 0)
        return cls(bx, by, w, h, bytearray(zlib.decompress(data[16:])))

    # ------------------------------------------------------------------ sorgular
    def to_node(self, x: float, y: float) -> tuple[int, int]:
        step = CELL_SIZE * NODE
        return int((x - self.base_x) // step), int((y - self.base_y) // step)

    def to_world(self, nx: int, ny: int) -> tuple[float, float]:
        step = CELL_SIZE * NODE
        return self.base_x + nx * step + step / 2, self.base_y + ny * step + step / 2

    def inside(self, nx: int, ny: int) -> bool:
        return 0 <= nx < self.w and 0 <= ny < self.h

    def walkable(self, nx: int, ny: int) -> bool:
        return self.inside(nx, ny) and self.walk[ny * self.w + nx] == 1

    def walkable_world(self, x: float, y: float) -> bool:
        return self.walkable(*self.to_node(x, y))

    def nearest_walkable(self, nx: int, ny: int, max_r: int = 30) -> tuple[int, int] | None:
        if self.walkable(nx, ny):
            return nx, ny
        for r in range(1, max_r + 1):
            for dx in range(-r, r + 1):
                for dy in (-r, r):
                    if self.walkable(nx + dx, ny + dy):
                        return nx + dx, ny + dy
            for dy in range(-r + 1, r):
                for dx in (-r, r):
                    if self.walkable(nx + dx, ny + dy):
                        return nx + dx, ny + dy
        return None

    def line_free(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        """a→b doğru parçası yalnızca yürünebilir düğümlerden geçiyor mu (Bresenham, çapraz köşe dahil)."""
        x0, y0 = a
        x1, y1 = b
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x1 > x0 else -1), (1 if y1 > y0 else -1)
        err = dx - dy
        while True:
            if not self.walkable(x0, y0):
                return False
            if (x0, y0) == (x1, y1):
                return True
            e2 = 2 * err
            if e2 > -dy and e2 < dx:          # çapraz adım: köşe kesmesin
                if not (self.walkable(x0 + sx, y0) and self.walkable(x0, y0 + sy)):
                    return False
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def find_path(self, start: tuple[float, float], goal: tuple[float, float], max_nodes: int = 400_000,
                  weight: float = 1.4) -> list[tuple[float, float]]:
        """Dünya koordinatlarında (x, y) ara noktalar; ilk nokta başlangıçtan sonraki ilk dönüş.
        Hedef engelliyse en yakın yürünebilir noktaya gidilir. Yol yoksa NavError."""
        s = self.nearest_walkable(*self.to_node(*start), max_r=5)
        g = self.nearest_walkable(*self.to_node(*goal))
        if s is None:
            raise NavError("Başlangıç noktası yürünemez bir yerde")
        if g is None:
            raise NavError("Hedefin çevresinde yürünebilir yer yok")
        if s == g:
            return [goal]
        if self.line_free(s, g):
            return [self.to_world(*g) if g != self.to_node(*goal) else goal]
        w = self.w
        sq2 = math.sqrt(2)
        gx, gy = g

        def h(x: int, y: int) -> float:
            ddx, ddy = abs(x - gx), abs(y - gy)
            return (max(ddx, ddy) + (sq2 - 1) * min(ddx, ddy)) * weight

        start_i = s[1] * w + s[0]
        goal_i = gy * w + gx
        came: dict[int, int] = {start_i: -1}
        cost: dict[int, float] = {start_i: 0.0}
        heap = [(h(*s), 0.0, start_i)]
        walk = self.walk
        found = False
        while heap:
            _, c, i = heapq.heappop(heap)
            if i == goal_i:
                found = True
                break
            if c > cost.get(i, math.inf):
                continue
            if len(came) > max_nodes:
                break
            x, y = i % w, i // w
            for ddx, ddy, step in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                                   (1, 1, sq2), (1, -1, sq2), (-1, 1, sq2), (-1, -1, sq2)):
                nx, ny = x + ddx, y + ddy
                if not (0 <= nx < w and 0 <= ny < self.h):
                    continue
                j = ny * w + nx
                if not walk[j]:
                    continue
                if ddx and ddy and not (walk[y * w + nx] and walk[ny * w + x]):
                    continue                  # köşe kesme yok
                nc = c + step
                if nc < cost.get(j, math.inf):
                    cost[j] = nc
                    came[j] = i
                    heapq.heappush(heap, (nc + h(nx, ny), nc, j))
        if not found:
            raise NavError("Hedefe yürüyerek ulaşılamıyor (yol yok ya da çok uzak)")
        nodes = []
        i = goal_i
        while i != -1:
            nodes.append((i % w, i // w))
            i = came[i]
        nodes.reverse()
        # Görüş hattı sadeleştirme: yalnızca dönüş noktaları kalsın
        pts = [nodes[0]]
        k = 0
        while k < len(nodes) - 1:
            j = len(nodes) - 1
            while j > k + 1 and not self.line_free(nodes[k], nodes[j]):
                j -= 1
            pts.append(nodes[j])
            k = j
        out = [self.to_world(*p) for p in pts[1:]]
        if self.walkable_world(*goal):
            out[-1] = goal
        return out


class NavStore:
    """Harita dizini (maps/<dosya>/server_attr) + önbellek (maps/<dosya>/nav.bin)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._grids: dict[str, NavGrid | None] = {}

    def grid(self, map_file: str, base_x: int, base_y: int) -> NavGrid | None:
        if map_file in self._grids:
            return self._grids[map_file]
        d = self.root / map_file
        cache, src = d / "nav.bin", d / "server_attr"
        g = None
        if cache.exists() and (not src.exists() or cache.stat().st_mtime >= src.stat().st_mtime):
            g = NavGrid.load(cache)
        elif src.exists():
            g = NavGrid.from_server_attr(src, base_x, base_y)
            g.save(cache)
        self._grids[map_file] = g
        return g
