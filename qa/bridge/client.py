"""Bridge istemcileri: TCP (gerçek QA client / sim-server) ve in-process (simülatör)."""

from __future__ import annotations

import itertools
import socket
from abc import ABC, abstractmethod
from typing import Any, Callable

from .protocol import ActionError, BridgeError, decode, encode


class Bridge(ABC):
    """Tek bir oyun istemcisine (tek karakter) bağlantı."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._events: list[dict[str, Any]] = []
        self.last_t: int = 0
        self.info: dict[str, Any] = {}

    @abstractmethod
    def _roundtrip(self, msg: dict[str, Any]) -> dict[str, Any]:
        """İsteği gönder, yanıtı döndür; aradaki olayları self._events'e ekle."""

    def close(self) -> None:  # pragma: no cover - alt sınıflar
        pass

    def connect(self) -> dict[str, Any]:
        self.info = self.call("hello")
        return self.info

    @property
    def capabilities(self) -> set[str]:
        return set(self.info.get("capabilities", []))

    def request(self, cmd: str, **args: Any) -> dict[str, Any]:
        msg = {"id": next(self._ids), "cmd": cmd, "args": args}
        resp = self._roundtrip(msg)
        if resp.get("id") != msg["id"]:
            raise BridgeError(f"Beklenmeyen yanıt id: {resp.get('id')} != {msg['id']}")
        if "t" in resp:
            self.last_t = int(resp["t"])
        return resp

    def call(self, cmd: str, **args: Any) -> Any:
        """Başarısız yanıtta ActionError fırlatır, başarılıysa data döner."""
        resp = self.request(cmd, **args)
        if not resp.get("ok"):
            e = resp.get("error") or {}
            raise ActionError(cmd, e.get("code", "ERROR"), e.get("message", ""), resp.get("t"))
        return resp.get("data")

    def drain_events(self) -> list[dict[str, Any]]:
        ev, self._events = self._events, []
        return ev

    def __enter__(self) -> "Bridge":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class LocalBridge(Bridge):
    """Simülatöre doğrudan (soketsiz) bağlanır. handler: msg -> [olaylar..., yanıt]."""

    def __init__(self, handler: Callable[[dict[str, Any]], list[dict[str, Any]]],
                 on_close: Callable[[], None] | None = None):
        super().__init__()
        self._handler = handler
        self._on_close = on_close

    def close(self) -> None:
        if self._on_close:
            self._on_close()
            self._on_close = None

    def _roundtrip(self, msg: dict[str, Any]) -> dict[str, Any]:
        # JSON'dan geçirerek gerçek protokolle aynı serileştirme kurallarını zorla
        out = [decode(encode(m)) for m in self._handler(decode(encode(msg)))]
        resp = None
        for m in out:
            if "event" in m:
                self._events.append(m)
            else:
                resp = m
        if resp is None:
            raise BridgeError(f"'{msg['cmd']}' için yanıt yok")
        return resp


class TcpBridge(Bridge):
    def __init__(self, host: str, port: int, timeout_s: float = 15.0):
        super().__init__()
        self.host, self.port, self.timeout_s = host, port, timeout_s
        self._sock: socket.socket | None = None
        self._buf = b""

    def _ensure(self) -> socket.socket:
        if self._sock is None:
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            except OSError as e:
                raise BridgeError(f"QA client'a bağlanılamadı {self.host}:{self.port}: {e}") from e
            self._buf = b""
        return self._sock

    def _readline(self, sock: socket.socket, timeout_s: float) -> bytes:
        sock.settimeout(timeout_s)
        while b"\n" not in self._buf:
            try:
                chunk = sock.recv(65536)
            except socket.timeout as e:
                raise BridgeError("QA client yanıt vermedi (timeout)") from e
            except OSError as e:
                raise BridgeError(f"Bağlantı hatası: {e}") from e
            if not chunk:
                self.close()
                raise BridgeError("QA client bağlantıyı kapattı")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def _roundtrip(self, msg: dict[str, Any]) -> dict[str, Any]:
        sock = self._ensure()
        try:
            sock.sendall(encode(msg))
        except OSError as e:
            self.close()
            raise BridgeError(f"Gönderilemedi: {e}") from e
        # `wait` uzun sürebilir: bekleme süresi + tolerans
        timeout = self.timeout_s
        if msg.get("cmd") == "wait":
            timeout += float(msg.get("args", {}).get("ms", 0)) / 1000.0
        while True:
            line = self._readline(sock, timeout)
            if not line.strip():
                continue
            m = decode(line)
            if "event" in m:
                self._events.append(m)
                continue
            return m

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
