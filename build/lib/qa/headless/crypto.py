"""Paket şifreleme eklentileri.

Metin2 fork'ları şifrelemede ayrışır:
  * none  — şifreleme kapalı (birçok özel sunucu, test ortamları)
  * xtea  — eski sürümlerin XTEA'sı (deneysel; anahtar ve blok hizalaması fork'a göre doğrulanmalı)
  * "geliştirilmiş şifreleme" (_IMPROVED_PACKET_ENCRYPTION_: Diffie-Hellman anahtar anlaşması + blok
    şifre) — kaynak kod geldiğinde fork'un KeyAgreement/Cipher uygulamasına göre eklenecek.
"""

from __future__ import annotations

import struct


class Crypto:
    name = "base"

    def encrypt(self, data: bytes) -> bytes:  # pragma: no cover - arayüz
        raise NotImplementedError

    def decrypt(self, data: bytes) -> bytes:  # pragma: no cover - arayüz
        raise NotImplementedError


class NoCrypto(Crypto):
    name = "none"

    def encrypt(self, data: bytes) -> bytes:
        return data

    def decrypt(self, data: bytes) -> bytes:
        return data


_DELTA = 0x9E3779B9
_MASK = 0xFFFFFFFF


def xtea_encipher_block(v: bytes, key: tuple[int, int, int, int], rounds: int = 32) -> bytes:
    v0, v1 = struct.unpack("<2I", v)
    s = 0
    for _ in range(rounds):
        v0 = (v0 + ((((v1 << 4) ^ (v1 >> 5)) + v1) ^ (s + key[s & 3]))) & _MASK
        s = (s + _DELTA) & _MASK
        v1 = (v1 + ((((v0 << 4) ^ (v0 >> 5)) + v0) ^ (s + key[(s >> 11) & 3]))) & _MASK
    return struct.pack("<2I", v0, v1)


def xtea_decipher_block(v: bytes, key: tuple[int, int, int, int], rounds: int = 32) -> bytes:
    v0, v1 = struct.unpack("<2I", v)
    s = (_DELTA * rounds) & _MASK
    for _ in range(rounds):
        v1 = (v1 - ((((v0 << 4) ^ (v0 >> 5)) + v0) ^ (s + key[(s >> 11) & 3]))) & _MASK
        s = (s - _DELTA) & _MASK
        v0 = (v0 - ((((v1 << 4) ^ (v1 >> 5)) + v1) ^ (s + key[s & 3]))) & _MASK
    return struct.pack("<2I", v0, v1)


class XteaCrypto(Crypto):
    """8 baytlık bloklarla XTEA akışı. Gönderilen veri 8'in katına sıfırla tamamlanır; gelen veri
    tam bloklar oluştukça çözülür. DENEYSEL: fork'un anahtar üretimi ve dolgu kuralıyla doğrulanmalı."""

    name = "xtea"

    def __init__(self, key: bytes, rounds: int = 32):
        if len(key) != 16:
            raise ValueError("XTEA anahtarı 16 bayt olmalı")
        self.key = struct.unpack("<4I", key)
        self.rounds = rounds
        self._in = b""

    def encrypt(self, data: bytes) -> bytes:
        if len(data) % 8:
            data += b"\0" * (8 - len(data) % 8)
        return b"".join(xtea_encipher_block(data[i:i + 8], self.key, self.rounds) for i in range(0, len(data), 8))

    def decrypt(self, data: bytes) -> bytes:
        self._in += data
        n = len(self._in) - len(self._in) % 8
        blocks, self._in = self._in[:n], self._in[n:]
        return b"".join(xtea_decipher_block(blocks[i:i + 8], self.key, self.rounds) for i in range(0, n, 8))


def make_crypto(name: str, key: bytes | None = None) -> Crypto:
    if name in ("none", "", None):
        return NoCrypto()
    if name == "xtea":
        if key is None:
            raise ValueError("xtea için anahtar gerekli")
        return XteaCrypto(key)
    raise ValueError(f"Desteklenmeyen şifreleme: {name!r} (none | xtea). Geliştirilmiş şifreleme "
                     "(_IMPROVED_PACKET_ENCRYPTION_) kaynak kod geldiğinde eklenecek.")
