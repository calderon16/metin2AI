"""Paket şifreleme eklentileri.

Metin2 fork'ları şifrelemede ayrışır:
  * none     — şifreleme kapalı (birçok özel sunucu, test ortamları)
  * xtea     — eski sürümlerin XTEA'sı (deneysel; anahtar ve blok hizalaması fork'a göre doğrulanmalı)
  * improved — "geliştirilmiş şifreleme" (_IMPROVED_PACKET_ENCRYPTION_): handshake'ten sonra sunucu
               HEADER_GC_KEY_AGREEMENT ile Diffie-Hellman (DH2: statik + geçici anahtar, RFC 5114 1024 bit
               grubu) başlatır; ortak sırdan her yön için bir blok şifre (bkz. blockciphers.py) CTR modunda
               seçilir. İstemci HEADER_CG_KEY_AGREEMENT'ı düz gönderir; HEADER_GC_KEY_AGREEMENT_COMPLETED
               paketinden sonraki her bayt iki yönde de şifrelidir.
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


# ---------------------------------------------------------------------- geliştirilmiş şifreleme

# RFC 5114 §2.1: 1024 bit MODP grubu, 160 bit asal mertebeli alt grup (sunucu cipher.cpp ile aynı)
DH_P = int(
    "B10B8F96A080E01DDE92DE5EAE5D54EC52C99FBCFB06A3C69A6A9DCA52D23B616073E28675A23D189838EF1E2EE652C0"
    "13ECB4AEA906112324975C3CD49B83BFACCBDD7D90C4BD7098488E9C219A73724EFFD6FAE5644738FAA31A4FF55BCCC0"
    "A151AF5F0DC8B4BD45BF37DF365C1A65E68CFDA76D4DA708DF1FB2BC2E4A4371", 16)
DH_G = int(
    "A4D1CBD5C3FD34126765A442EFB99905F8104DD258AC507FD6406CFF14266D31266FEA1E5C41564B777E690F5504F213"
    "160217B4B01B886A5E91547F9E2749F4D7FBD7D3B9A92EE1909D0D2263F80A76A6A24C087A091F531DBF0A0169B6A28A"
    "D662A4D18E73AFA32D779D5918D08BC8858F4DCEF97C2A24855E6EEB22B3B2E5", 16)
DH_Q = int("F518AA8781A8DF278ABA4E7D64B7CB9D49462353", 16)
DH_ELEMENT_LEN = 128                      # modül uzunluğu (bayt)
DH2_AGREED_LEN = 2 * DH_ELEMENT_LEN       # statik + geçici ortak değer
DH2_DATA_LEN = 2 * DH_ELEMENT_LEN         # statik + geçici açık anahtar


class KeyAgreementError(Exception):
    pass


def _valid_element(y: int) -> bool:
    return 1 < y < DH_P - 1 and pow(y, DH_Q, DH_P) == 1


class ImprovedCrypto(Crypto):
    """Anlaşmadan sonra etkin iki yönlü CTR akışı (cipher.cpp `Cipher::SetUp`)."""

    name = "improved"

    def __init__(self, shared: bytes, polarity: bool = True):
        from .blockciphers import CtrStream, pick

        n = len(shared)
        if n < 2:
            raise KeyAgreementError("Ortak sır çok kısa")
        alg0 = pick(shared[shared[0] % n])
        alg1 = pick(shared[shared[1] % n])
        kl0, kl1 = alg0.key_size, alg1.key_size
        bs0, bs1 = alg0.block_size, alg1.block_size
        if n < max(kl0, kl1, bs0, bs1):
            raise KeyAgreementError("Ortak sır anahtar/IV için kısa")
        key0 = shared[:kl0]
        off = min(kl0, n - kl1)
        key1 = shared[off:off + kl1]
        off = n - bs0
        iv0 = shared[off:off + bs0]
        off = 0 if off < bs1 else off - bs1
        iv1 = shared[off:off + bs1]
        c0, c1 = alg0(key0), alg1(key1)
        self.algorithms = (c0.name, c1.name)
        if polarity:    # istemci: gönderirken 1., alırken 0. algoritma
            self._enc, self._dec = CtrStream(c1, iv1), CtrStream(c0, iv0)
        else:           # sunucu
            self._enc, self._dec = CtrStream(c0, iv0), CtrStream(c1, iv1)

    def encrypt(self, data: bytes) -> bytes:
        return self._enc.process(data)

    def decrypt(self, data: bytes) -> bytes:
        return self._dec.process(data)


class ImprovedKeyAgreement:
    """İstemci tarafı DH2 (Crypto++ `DH2`): statik ve geçici anahtar çifti; ortak sır iki DH değerinin birleşimi."""

    def __init__(self, static_private: int | None = None, ephemeral_private: int | None = None):
        import secrets

        self.static_private = static_private or (secrets.randbelow(DH_Q - 1) + 1)
        self.ephemeral_private = ephemeral_private or (secrets.randbelow(DH_Q - 1) + 1)
        self.static_public = pow(DH_G, self.static_private, DH_P)
        self.ephemeral_public = pow(DH_G, self.ephemeral_private, DH_P)

    @property
    def data(self) -> bytes:
        """HEADER_CG_KEY_AGREEMENT.data: statik açık anahtar + geçici açık anahtar (büyük endian, 128'er bayt)."""
        return self.static_public.to_bytes(DH_ELEMENT_LEN, "big") + self.ephemeral_public.to_bytes(DH_ELEMENT_LEN, "big")

    def shared(self, agreed_length: int, peer: bytes) -> bytes:
        if agreed_length != DH2_AGREED_LEN:
            raise KeyAgreementError(f"Beklenmeyen anlaşma uzunluğu {agreed_length} (beklenen {DH2_AGREED_LEN})")
        if len(peer) != DH2_DATA_LEN:
            raise KeyAgreementError(f"Beklenmeyen anahtar verisi uzunluğu {len(peer)} (beklenen {DH2_DATA_LEN})")
        ys = int.from_bytes(peer[:DH_ELEMENT_LEN], "big")
        ye = int.from_bytes(peer[DH_ELEMENT_LEN:], "big")
        if not (_valid_element(ys) and _valid_element(ye)):
            raise KeyAgreementError("Sunucunun açık anahtarı DH grubunda değil")
        return pow(ys, self.static_private, DH_P).to_bytes(DH_ELEMENT_LEN, "big")             + pow(ye, self.ephemeral_private, DH_P).to_bytes(DH_ELEMENT_LEN, "big")

    def agree(self, agreed_length: int, peer: bytes) -> ImprovedCrypto:
        return ImprovedCrypto(self.shared(agreed_length, peer), polarity=True)


CRYPTO_NAMES = ("none", "xtea", "improved")


def make_crypto(name: str, key: bytes | None = None) -> Crypto:
    """Bağlantının başlangıç şifrelemesi. `improved` düz başlar; anahtar anlaşmasıyla ImprovedCrypto'ya geçer."""
    if name in ("none", "", None, "improved"):
        return NoCrypto()
    if name == "xtea":
        if key is None:
            raise ValueError("xtea için anahtar gerekli")
        return XteaCrypto(key)
    raise ValueError(f"Desteklenmeyen şifreleme: {name!r} ({' | '.join(CRYPTO_NAMES)})")
