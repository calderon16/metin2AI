"""Metin2 paket bağlantısı: soket + profile göre çerçeveleme + eklenebilir şifreleme.

Gelen (GC) paketlerin boyutu profilden bulunur: statik paket = struct boyutu, dinamik paket =
header'dan sonraki WORD `size` (header dahil toplam). Bilinmeyen bir header gelirse akış
senkronunu kaybettiğimiz için bağlantı ProtocolError ile kapatılır (profil/fork uyumsuzluğu).
"""

from __future__ import annotations

import select
import socket
import struct
import time
from typing import Any, Callable

from .crypto import Crypto, NoCrypto
from .profile import Profile, ProfileError


class ProtocolError(Exception):
    pass


class Packet:
    __slots__ = ("name", "data", "trailing")

    def __init__(self, name: str, data: dict[str, Any], trailing: bytes = b""):
        self.name, self.data, self.trailing = name, data, trailing

    def __repr__(self) -> str:  # pragma: no cover - hata ayıklama
        return f"Packet({self.name}, {self.data}, +{len(self.trailing)}B)"


class PacketConnection:
    def __init__(self, profile: Profile, host: str, port: int, timeout_s: float = 15.0,
                 crypto: Crypto | None = None, direction_in: str = "GC", direction_out: str = "CG",
                 log: Callable[[str], None] | None = None):
        self.profile = profile
        self.host, self.port, self.timeout_s = host, port, timeout_s
        self.crypto = crypto or NoCrypto()
        self.dir_in, self.dir_out = direction_in, direction_out
        self._log = log or (lambda s: None)
        self.sock: socket.socket | None = None
        self._buf = b""
        self.bytes_in = self.bytes_out = 0

    @classmethod
    def from_socket(cls, profile: Profile, sock: socket.socket, direction_in: str, direction_out: str,
                    log: Callable[[str], None] | None = None) -> "PacketConnection":
        """Kabul edilmiş bir soketi sarmala (test sunucusu için: gelen CG, giden GC)."""
        c = cls(profile, "", 0, direction_in=direction_in, direction_out=direction_out, log=log)
        c.sock = sock
        return c

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        except OSError as e:
            raise ProtocolError(f"{self.host}:{self.port} bağlanılamadı: {e}") from e
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buf = b""
        self._log(f"bağlandı {self.host}:{self.port}")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None
                self._log(f"bağlantı kapandı {self.host}:{self.port}")

    # ------------------------------------------------------------------ gönderme
    def build(self, header_name: str, values: dict[str, Any] | None = None, trailing: bytes = b"") -> bytes:
        prof = self.profile
        st_name = prof.struct_for(header_name)
        st = prof.structs[prof.resolve(st_name)]
        vals = dict(values or {})
        vals[st.fields[0].name] = prof.header(header_name)     # ilk alan her zaman header
        if prof.packets[header_name].get("dynamic") or trailing:
            size_field = next((f.name for f in st.fields[1:2] if f.name.lower() in ("size", "wsize", "length")), None)
            if size_field:
                vals[size_field] = st.size + len(trailing)
        return prof.encode(st_name, vals) + trailing

    def send(self, header_name: str, values: dict[str, Any] | None = None, trailing: bytes = b"") -> None:
        if self.sock is None:
            raise ProtocolError("Bağlı değil")
        raw = self.build(header_name, values, trailing)
        try:
            self.sock.sendall(self.crypto.encrypt(raw))
        except OSError as e:
            self.close()
            raise ProtocolError(f"Gönderilemedi: {e}") from e
        self.bytes_out += len(raw)

    # ------------------------------------------------------------------ alma
    def poll(self, timeout_s: float = 0.0) -> list[Packet]:
        """Gelen veriyi oku (en fazla timeout kadar bekle) ve tamamlanmış paketleri döndür."""
        if self.sock is None:
            raise ProtocolError("Bağlı değil")
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            wait = max(0.0, deadline - time.monotonic())
            r, _, _ = select.select([self.sock], [], [], wait)
            if not r:
                break
            try:
                chunk = self.sock.recv(65536)
            except OSError as e:
                self.close()
                raise ProtocolError(f"Okuma hatası: {e}") from e
            if not chunk:
                self.close()
                raise ProtocolError("Sunucu bağlantıyı kapattı")
            self._buf += self.crypto.decrypt(chunk)
            self.bytes_in += len(chunk)
            if self._complete_available():
                break
        return self._drain()

    def _frame_len(self) -> int | None:
        if not self._buf:
            return None
        hv = self._buf[0]
        name = self.profile.packet_by_value(self.dir_in, hv)
        if name is None:
            raise ProtocolError(f"Bilinmeyen {self.dir_in} header: {hv} (0x{hv:02x}) — profil fork ile uyumsuz olabilir")
        info = self.profile.packets[name]
        if not info.get("struct"):
            raise ProtocolError(f"{name} için struct bilinmiyor — boyut tablosunu profile ekleyin")
        if info.get("dynamic"):
            if len(self._buf) < 3:
                return None
            return struct.unpack_from("<H", self._buf, 1)[0]
        return self.profile.sizeof(info["struct"])

    def _complete_available(self) -> bool:
        try:
            n = self._frame_len()
        except ProtocolError:
            return True
        return n is not None and len(self._buf) >= n

    def _drain(self) -> list[Packet]:
        out = []
        while True:
            n = self._frame_len()
            if n is None or len(self._buf) < n:
                break
            if n <= 0:
                raise ProtocolError("Geçersiz paket boyutu 0")
            frame, self._buf = self._buf[:n], self._buf[n:]
            name = self.profile.packet_by_value(self.dir_in, frame[0])
            st = self.profile.packets[name]["struct"]
            try:
                data, off = self.profile.decode(st, frame)
            except ProfileError as e:
                raise ProtocolError(f"{name} çözülemedi: {e}") from e
            out.append(Packet(name, data, frame[off:]))
        return out
