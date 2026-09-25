"""`_IMPROVED_PACKET_ENCRYPTION_` için blok şifreler ve CTR modu.

Sunucu (game/src/cipher.cpp, Crypto++) anahtar anlaşmasından çıkan ortak sırrın iki baytına göre her yön için
14 seçenekten birini seçer (`BlockCipherAlgorithm::Pick`): Twofish (varsayılan), RC6, MARS, Twofish, Serpent,
CAST-256, IDEA, 3DES (DES-EDE2), Camellia, SEED, RC5, Blowfish, TEA, SHACAL-2. Hepsi 16 baytlık varsayılan
anahtarla CTR modunda kullanılır.

IDEA, 3DES, Camellia, SEED ve Blowfish için `cryptography` (OpenSSL) kullanılır; diğerleri saf Python'dur
ve Crypto++'ın bayt/kelime sırası kurallarına uyar. Doğruluk tests/data/cryptopp_*.txt vektörleriyle
(sunucunun kendi Crypto++ kitaplığıyla üretildi) sınanır.
"""

from __future__ import annotations

import struct
import warnings
from typing import Callable

from . import _cipher_tables as T

M32 = 0xFFFFFFFF


def _rotl(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & M32 if n else x


def _rotr(x: int, n: int) -> int:
    n &= 31
    return ((x >> n) | (x << (32 - n))) & M32 if n else x


class BlockCipher:
    name = "base"
    block_size = 16
    key_size = 16

    def encrypt_block(self, block: bytes) -> bytes:  # pragma: no cover - arayüz
        raise NotImplementedError


# ---------------------------------------------------------------------- OpenSSL (cryptography) ile

class _OpenSSLCipher(BlockCipher):
    def __init__(self, key: bytes):
        from cryptography.hazmat.primitives.ciphers import Cipher, modes

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._enc = Cipher(self._algorithm(key), modes.ECB()).encryptor()

    def _algorithm(self, key: bytes):  # pragma: no cover - arayüz
        raise NotImplementedError

    def encrypt_block(self, block: bytes) -> bytes:
        return self._enc.update(block)


def _decrepit():
    from cryptography.hazmat.decrepit.ciphers import algorithms

    return algorithms


class Camellia(_OpenSSLCipher):
    name = "camellia"

    def _algorithm(self, key: bytes):
        return _decrepit().Camellia(key)


class Seed(_OpenSSLCipher):
    name = "seed"

    def _algorithm(self, key: bytes):
        return _decrepit().SEED(key)


class Idea(_OpenSSLCipher):
    name = "idea"
    block_size = 8

    def _algorithm(self, key: bytes):
        return _decrepit().IDEA(key)


class Blowfish(_OpenSSLCipher):
    name = "blowfish"
    block_size = 8

    def _algorithm(self, key: bytes):
        return _decrepit().Blowfish(key)


class DesEde2(_OpenSSLCipher):
    """İki anahtarlı 3DES (K1, K2, K1)."""

    name = "3des"
    block_size = 8

    def _algorithm(self, key: bytes):
        return _decrepit().TripleDES(key + key[:8])


# ---------------------------------------------------------------------- saf Python

class Tea(BlockCipher):
    """Crypto++ TEA: büyük endian blok ve anahtar, 32 tur."""

    name = "tea"
    block_size = 8
    _DELTA = 0x9E3779B9

    def __init__(self, key: bytes):
        self.k = struct.unpack(">4I", key)

    def encrypt_block(self, block: bytes) -> bytes:
        y, z = struct.unpack(">2I", block)
        k0, k1, k2, k3 = self.k
        s = 0
        for _ in range(32):
            s = (s + self._DELTA) & M32
            y = (y + ((((z << 4) & M32) + k0) ^ (z + s) ^ ((z >> 5) + k1))) & M32
            z = (z + ((((y << 4) & M32) + k2) ^ (y + s) ^ ((y >> 5) + k3))) & M32
        return struct.pack(">2I", y, z)


def _rc_key_schedule(key: bytes, size: int) -> list[int]:
    """RC5/RC6 anahtar genişletme (32 bit kelime, küçük endian)."""
    c = max((len(key) + 3) // 4, 1)
    L = list(struct.unpack(f"<{c}I", key.ljust(4 * c, b"\0")))
    S = [0xB7E15163]
    for _ in range(1, size):
        S.append((S[-1] + 0x9E3779B9) & M32)
    a = b = 0
    for h in range(3 * max(size, c)):
        a = S[h % size] = _rotl((S[h % size] + a + b) & M32, 3)
        b = L[h % c] = _rotl((L[h % c] + a + b) & M32, (a + b) & 31)
    return S


class Rc5(BlockCipher):
    """RC5-32/16 (Crypto++ varsayılanı 16 tur)."""

    name = "rc5"
    block_size = 8
    ROUNDS = 16

    def __init__(self, key: bytes):
        self.S = _rc_key_schedule(key, 2 * (self.ROUNDS + 1))

    def encrypt_block(self, block: bytes) -> bytes:
        S = self.S
        a, b = struct.unpack("<2I", block)
        a = (a + S[0]) & M32
        b = (b + S[1]) & M32
        for i in range(1, self.ROUNDS + 1):
            a = (_rotl(a ^ b, b) + S[2 * i]) & M32
            b = (_rotl(b ^ a, a) + S[2 * i + 1]) & M32
        return struct.pack("<2I", a, b)


class Rc6(BlockCipher):
    """RC6-32/20."""

    name = "rc6"
    ROUNDS = 20

    def __init__(self, key: bytes):
        self.S = _rc_key_schedule(key, 2 * self.ROUNDS + 4)

    def encrypt_block(self, block: bytes) -> bytes:
        S = self.S
        a, b, c, d = struct.unpack("<4I", block)
        b = (b + S[0]) & M32
        d = (d + S[1]) & M32
        for i in range(1, self.ROUNDS + 1):
            t = _rotl((b * (2 * b + 1)) & M32, 5)
            u = _rotl((d * (2 * d + 1)) & M32, 5)
            a = (_rotl(a ^ t, u) + S[2 * i]) & M32
            c = (_rotl(c ^ u, t) + S[2 * i + 1]) & M32
            a, b, c, d = b, c, d, a
        a = (a + S[2 * self.ROUNDS + 2]) & M32
        c = (c + S[2 * self.ROUNDS + 3]) & M32
        return struct.pack("<4I", a, b, c, d)


# SHA-256 sabitleri
_SHA_K = (
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
)


class Shacal2(BlockCipher):
    """SHACAL-2: SHA-256 sıkıştırma turları; 32 baytlık blok, anahtar 64 bayta sıfırla tamamlanır."""

    name = "shacal2"
    block_size = 32

    def __init__(self, key: bytes):
        w = list(struct.unpack(">16I", key.ljust(64, b"\0")))
        for i in range(16, 64):
            x, y = w[i - 15], w[i - 2]
            s0 = _rotr(x, 7) ^ _rotr(x, 18) ^ (x >> 3)
            s1 = _rotr(y, 17) ^ _rotr(y, 19) ^ (y >> 10)
            w.append((w[i - 16] + s0 + w[i - 7] + s1) & M32)
        self.rk = [(w[i] + _SHA_K[i]) & M32 for i in range(64)]

    def encrypt_block(self, block: bytes) -> bytes:
        a, b, c, d, e, f, g, h = struct.unpack(">8I", block)
        for k in self.rk:
            t1 = (h + (_rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)) + (g ^ (e & (f ^ g))) + k) & M32
            t2 = ((_rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)) + ((a & b) | (c & (a | b)))) & M32
            a, b, c, d, e, f, g, h = (t1 + t2) & M32, a, b, c, (d + t1) & M32, e, f, g
        return struct.pack(">8I", a, b, c, d, e, f, g, h)


class Cast256(BlockCipher):
    """CAST-256 (RFC 2612): büyük endian; 16 baytlık anahtar 32 bayta sıfırla tamamlanır."""

    name = "cast256"

    def __init__(self, key: bytes):
        k = list(struct.unpack(">8I", key.ljust(32, b"\0")))
        cm, cr = 0x5A827999, 19
        tm = [[0] * 24 for _ in range(8)]
        tr = [[0] * 24 for _ in range(8)]
        for i in range(24):
            for j in range(8):
                tm[j][i], tr[j][i] = cm, cr
                cm = (cm + 0x6ED9EBA1) & M32
                cr = (cr + 17) & 31
        self.km: list[tuple[int, int, int, int]] = []
        self.kr: list[tuple[int, int, int, int]] = []
        for i in range(12):
            for r in (2 * i, 2 * i + 1):
                a, b, c, d, e, f, g, h = k
                g ^= self._f1(h, tm[0][r], tr[0][r])
                f ^= self._f2(g, tm[1][r], tr[1][r])
                e ^= self._f3(f, tm[2][r], tr[2][r])
                d ^= self._f1(e, tm[3][r], tr[3][r])
                c ^= self._f2(d, tm[4][r], tr[4][r])
                b ^= self._f3(c, tm[5][r], tr[5][r])
                a ^= self._f1(b, tm[6][r], tr[6][r])
                h ^= self._f2(a, tm[7][r], tr[7][r])
                k = [a, b, c, d, e, f, g, h]
            a, b, c, d, e, f, g, h = k
            self.kr.append((a & 31, c & 31, e & 31, g & 31))
            self.km.append((h, f, d, b))

    @staticmethod
    def _f1(d: int, km: int, kr: int) -> int:
        i = _rotl((km + d) & M32, kr)
        return ((((T.CAST_S1[i >> 24] ^ T.CAST_S2[(i >> 16) & 255]) - T.CAST_S3[(i >> 8) & 255]) & M32)
                + T.CAST_S4[i & 255]) & M32

    @staticmethod
    def _f2(d: int, km: int, kr: int) -> int:
        i = _rotl(km ^ d, kr)
        return ((((T.CAST_S1[i >> 24] - T.CAST_S2[(i >> 16) & 255]) & M32) + T.CAST_S3[(i >> 8) & 255]) & M32) \
            ^ T.CAST_S4[i & 255]

    @staticmethod
    def _f3(d: int, km: int, kr: int) -> int:
        i = _rotl((km - d) & M32, kr)
        return ((((T.CAST_S1[i >> 24] + T.CAST_S2[(i >> 16) & 255]) & M32) ^ T.CAST_S3[(i >> 8) & 255])
                - T.CAST_S4[i & 255]) & M32

    def encrypt_block(self, block: bytes) -> bytes:
        a, b, c, d = struct.unpack(">4I", block)
        f1, f2, f3 = self._f1, self._f2, self._f3
        for i in range(12):
            km, kr = self.km[i], self.kr[i]
            if i < 6:
                c ^= f1(d, km[0], kr[0])
                b ^= f2(c, km[1], kr[1])
                a ^= f3(b, km[2], kr[2])
                d ^= f1(a, km[3], kr[3])
            else:
                d ^= f1(a, km[3], kr[3])
                a ^= f3(b, km[2], kr[2])
                b ^= f2(c, km[1], kr[1])
                c ^= f1(d, km[0], kr[0])
        return struct.pack(">4I", a, b, c, d)


class Mars(BlockCipher):
    """MARS (IBM, AES adayı): küçük endian, 40 kelimelik anahtar genişletmesi."""

    name = "mars"

    def __init__(self, key: bytes):
        S = T.MARS_SBOX
        n = len(key) // 4
        t = list(struct.unpack(f"<{n}I", key)) + [0] * (15 - n)
        t[n] = n
        k = [0] * 40
        for j in range(4):
            for i in range(15):
                t[i] = t[i] ^ _rotl(t[(i + 8) % 15] ^ t[(i + 13) % 15], 3) ^ (4 * i + j)
            for _ in range(4):
                for i in range(15):
                    t[i] = _rotl((t[i] + S[t[(i + 14) % 15] & 511]) & M32, 9)
            for i in range(10):
                k[10 * j + i] = t[(4 * i) % 15]
        for i in range(5, 37, 2):
            w = k[i] | 3
            m = (~w ^ (w << 1)) & (~w ^ (w >> 1)) & 0x7FFFFFFE
            m &= m >> 1
            m &= m >> 2
            m &= m >> 4
            m |= m << 1
            m |= m << 2
            m |= m << 4
            m &= 0x7FFFFFFC
            w ^= _rotl(S[265 + (k[i] & 3)], k[i - 1] & 31) & m
            k[i] = w & M32
        self.k = k

    def encrypt_block(self, block: bytes) -> bytes:
        S, k = T.MARS_SBOX, self.k
        a, b, c, d = struct.unpack("<4I", block)
        a, b, c, d = (a + k[0]) & M32, (b + k[1]) & M32, (c + k[2]) & M32, (d + k[3]) & M32
        # ileri karıştırma
        for i in range(8):
            b = ((b ^ S[a & 255]) + S[256 + ((a >> 8) & 255)]) & M32
            c = (c + S[(a >> 16) & 255]) & M32
            d ^= S[256 + (a >> 24)]
            a = _rotr(a, 24)
            if i % 4 == 0:
                a = (a + d) & M32
            elif i % 4 == 1:
                a = (a + b) & M32
            a, b, c, d = b, c, d, a
        # şifreleme çekirdeği
        for i in range(16):
            r = _rotl((_rotl(a, 13) * k[2 * i + 5]) & M32, 5)
            m = (a + k[2 * i + 4]) & M32
            ll = S[m & 511] ^ r
            m = _rotl(m, r & 31)
            r = _rotl(r, 5)
            ll = _rotl(ll ^ r, r & 31)
            a = _rotl(a, 13)
            c = (c + m) & M32
            if i < 8:
                b = (b + ll) & M32
                d ^= r
            else:
                d = (d + ll) & M32
                b ^= r
            a, b, c, d = b, c, d, a
        # geri karıştırma
        for i in range(8):
            if i % 4 == 2:
                a = (a - d) & M32
            elif i % 4 == 3:
                a = (a - b) & M32
            b ^= S[256 + (a & 255)]
            c = (c - S[a >> 24]) & M32
            d = (d - S[256 + ((a >> 16) & 255)]) & M32
            d ^= S[(a >> 8) & 255]
            a = _rotl(a, 24)
            a, b, c, d = b, c, d, a
        a, b, c, d = (a - k[36]) & M32, (b - k[37]) & M32, (c - k[38]) & M32, (d - k[39]) & M32
        return struct.pack("<4I", a, b, c, d)


class Twofish(BlockCipher):
    name = "twofish"

    def __init__(self, key: bytes):
        n = 2 if len(key) <= 16 else (3 if len(key) <= 24 else 4)
        kw = list(struct.unpack(f"<{2 * n}I", key.ljust(8 * n, b"\0")))
        self.n = n
        self.k = [0] * 40
        for i in range(0, 40, 2):
            a = self._h(i, kw[0::2], n)
            b = _rotl(self._h(i + 1, kw[1::2], n), 8)
            self.k[i] = (a + b) & M32
            self.k[i + 1] = _rotl((a + 2 * b) & M32, 9)
        svec = [0] * n
        for i in range(n):
            svec[n - i - 1] = self._rs(kw[2 * i + 1], kw[2 * i])
        mds = (T.TWOFISH_MDS0, T.TWOFISH_MDS1, T.TWOFISH_MDS2, T.TWOFISH_MDS3)
        self.s = [[0] * 256 for _ in range(4)]
        for i in range(256):
            t = self._h0(i, svec, n)
            for j in range(4):
                self.s[j][i] = mds[j][(t >> (8 * j)) & 255]

    @staticmethod
    def _h0(x: int, key: list[int], n: int) -> int:
        q = (T.TWOFISH_Q0, T.TWOFISH_Q1)

        def Q(a: int, b: int, c: int, d: int, t: int) -> int:
            return q[a][t & 255] ^ (q[b][(t >> 8) & 255] << 8) ^ (q[c][(t >> 16) & 255] << 16) \
                ^ (q[d][t >> 24] << 24)

        x = x | (x << 8) | (x << 16) | (x << 24)
        if n == 4:
            x = Q(1, 0, 0, 1, x) ^ key[3]
        if n >= 3:
            x = Q(1, 1, 0, 0, x) ^ key[2]
        x = Q(0, 1, 0, 1, x) ^ key[1]
        x = Q(0, 0, 1, 1, x) ^ key[0]
        return x

    @classmethod
    def _h(cls, x: int, key: list[int], n: int) -> int:
        x = cls._h0(x, key, n)
        return T.TWOFISH_MDS0[x & 255] ^ T.TWOFISH_MDS1[(x >> 8) & 255] ^ T.TWOFISH_MDS2[(x >> 16) & 255] \
            ^ T.TWOFISH_MDS3[x >> 24]

    @staticmethod
    def _rs(high: int, low: int) -> int:
        def mod(c: int) -> int:
            c2 = ((c << 1) ^ (0x14D if c & 0x80 else 0)) & 0xFF
            c1 = c2 ^ (c >> 1) ^ ((0x14D >> 1) if c & 1 else 0)
            return c | (c1 << 8) | (c2 << 16) | (c1 << 24)

        for _ in range(8):
            high = (mod(high >> 24) ^ (high << 8) ^ (low >> 24)) & M32
            low = (low << 8) & M32
        return high

    def encrypt_block(self, block: bytes) -> bytes:
        s0, s1, s2, s3 = self.s
        k = self.k
        a, b, c, d = struct.unpack("<4I", block)
        a ^= k[0]
        b ^= k[1]
        c ^= k[2]
        d ^= k[3]
        for r in range(16):
            x = s0[a & 255] ^ s1[(a >> 8) & 255] ^ s2[(a >> 16) & 255] ^ s3[a >> 24]
            y = s0[b >> 24] ^ s1[b & 255] ^ s2[(b >> 8) & 255] ^ s3[(b >> 16) & 255]
            x = (x + y) & M32
            y = (y + x + k[2 * r + 9]) & M32
            c = _rotr(c ^ ((x + k[2 * r + 8]) & M32), 1)
            d = _rotl(d, 1) ^ y
            a, b, c, d = c, d, a, b   # 16 takastan sonra adlar Crypto++'takiyle aynı
        c ^= k[4]
        d ^= k[5]
        a ^= k[6]
        b ^= k[7]
        return struct.pack("<4I", c, d, a, b)


_SERPENT_SBOX = (
    (3, 8, 15, 1, 10, 6, 5, 11, 14, 13, 4, 2, 7, 0, 9, 12),
    (15, 12, 2, 7, 9, 0, 5, 10, 1, 11, 14, 8, 6, 13, 3, 4),
    (8, 6, 7, 9, 3, 12, 10, 15, 13, 1, 14, 4, 0, 11, 5, 2),
    (0, 15, 11, 8, 12, 9, 6, 3, 13, 1, 2, 4, 10, 7, 5, 14),
    (1, 15, 8, 3, 12, 0, 11, 6, 2, 5, 4, 10, 9, 14, 7, 13),
    (15, 5, 2, 11, 4, 10, 9, 12, 0, 3, 14, 8, 13, 6, 7, 1),
    (7, 2, 12, 5, 8, 4, 6, 11, 14, 9, 1, 15, 13, 3, 10, 0),
    (1, 13, 15, 0, 14, 8, 2, 11, 7, 4, 12, 10, 9, 3, 5, 6),
)
# bayt → her biti 4 bit aralıkla yayılmış 32 bit (bit i → bit 4i)
_SPREAD = tuple(sum(((b >> i) & 1) << (4 * i) for i in range(8)) for b in range(256))
# S kutusunu bir baytın iki nibble'ına birden uygulayan tablolar
_SERPENT_SBOX8 = tuple(tuple(s[b & 15] | (s[b >> 4] << 4) for b in range(256)) for s in _SERPENT_SBOX)


def _gather(v: int) -> int:
    """0,4,8,...,28 numaralı bitleri 0..7'ye topla."""
    v &= 0x11111111
    v = (v | (v >> 3)) & 0x03030303
    v = (v | (v >> 6)) & 0x000F000F
    return (v | (v >> 12)) & 0xFF


def _serpent_sbox(box: int, x0: int, x1: int, x2: int, x3: int) -> tuple[int, int, int, int]:
    """Bit dilimli S kutusu: j. bitlerden (x0 en düşük) oluşan nibble'a S uygulanır."""
    sb = _SERPENT_SBOX8[box]
    sp = _SPREAD
    out = [0, 0, 0, 0]
    for byte in range(4):
        sh = 8 * byte
        n = sp[(x0 >> sh) & 255] | (sp[(x1 >> sh) & 255] << 1) | (sp[(x2 >> sh) & 255] << 2) \
            | (sp[(x3 >> sh) & 255] << 3)
        r = sb[n & 255] | (sb[(n >> 8) & 255] << 8) | (sb[(n >> 16) & 255] << 16) | (sb[n >> 24] << 24)
        for k in range(4):
            out[k] |= _gather(r >> k) << sh
    return out[0], out[1], out[2], out[3]


class Serpent(BlockCipher):
    """Serpent (Crypto++/NESSIE bayt sırası: küçük endian kelimeler, bit dilimli)."""

    name = "serpent"

    def __init__(self, key: bytes):
        k0 = list(struct.unpack("<8I", key.ljust(32, b"\0")))
        if len(key) < 32:
            k0[len(key) // 4] |= 1 << ((len(key) % 4) * 8)
        w = k0[:]
        for i in range(132):
            w.append(_rotl(w[i] ^ w[i + 3] ^ w[i + 5] ^ w[i + 7] ^ 0x9E3779B9 ^ i, 11))
        pre = w[8:]
        self.rk = []
        for i in range(33):
            self.rk.append(_serpent_sbox((3 - i) % 8, *pre[4 * i:4 * i + 4]))

    def encrypt_block(self, block: bytes) -> bytes:
        x0, x1, x2, x3 = struct.unpack("<4I", block)
        rk = self.rk
        for r in range(32):
            k = rk[r]
            x0, x1, x2, x3 = _serpent_sbox(r % 8, x0 ^ k[0], x1 ^ k[1], x2 ^ k[2], x3 ^ k[3])
            if r < 31:
                x0 = _rotl(x0, 13)
                x2 = _rotl(x2, 3)
                x1 ^= x0 ^ x2
                x3 ^= x2 ^ ((x0 << 3) & M32)
                x1 = _rotl(x1, 1)
                x3 = _rotl(x3, 7)
                x0 ^= x1 ^ x3
                x2 ^= x3 ^ ((x1 << 7) & M32)
                x0 = _rotl(x0, 5)
                x2 = _rotl(x2, 22)
        k = rk[32]
        return struct.pack("<4I", x0 ^ k[0], x1 ^ k[1], x2 ^ k[2], x3 ^ k[3])


# ---------------------------------------------------------------------- seçim ve CTR

# cipher.cpp BlockCipherAlgorithm enum sırası (kAES yorum satırı; kSKIPJACK yok)
ALGORITHMS: tuple[Callable[[bytes], BlockCipher], ...] = (
    Twofish,    # kDefault
    Rc6, Mars, Twofish, Serpent, Cast256,
    Idea, DesEde2, Camellia, Seed, Rc5, Blowfish, Tea, Shacal2,
)


def pick(hint: int) -> Callable[[bytes], BlockCipher]:
    return ALGORITHMS[hint % len(ALGORITHMS)]


class CtrStream:
    """Crypto++ CTR_Mode: sayaç = IV, tüm blok büyük endian artar; anahtar akışı çağrılar arasında sürer."""

    def __init__(self, cipher: BlockCipher, iv: bytes):
        if len(iv) != cipher.block_size:
            raise ValueError("IV blok boyunda olmalı")
        self.cipher = cipher
        self.bs = cipher.block_size
        self.counter = int.from_bytes(iv, "big")
        self.mod = 1 << (8 * self.bs)
        self.ks = b""

    def process(self, data: bytes) -> bytes:
        if not data:
            return b""
        need = len(data) - len(self.ks)
        if need > 0:
            parts = [self.ks]
            for _ in range(-(-need // self.bs)):
                parts.append(self.cipher.encrypt_block(self.counter.to_bytes(self.bs, "big")))
                self.counter = (self.counter + 1) % self.mod
            self.ks = b"".join(parts)
        ks, self.ks = self.ks[:len(data)], self.ks[len(data):]
        n = len(data)
        return (int.from_bytes(data, "little") ^ int.from_bytes(ks, "little")).to_bytes(n, "little")
