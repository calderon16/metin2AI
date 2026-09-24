"""Simülatörü TCP üzerinden sunar — gerçek `Metin2_QA.exe` bridge'inin yerine geçer.

    python -m qa.sim.server --port 47800 --seed 1 --fault no_drop

Her bağlantı ayrı bir istemcidir (ayrı karakter); dünya ortaktır. Çoklu ajan testleri
(trade, party ...) bu şekilde birden fazla bağlantıyla denenebilir.
"""

from __future__ import annotations

import argparse
import socketserver
import threading

from ..bridge.protocol import decode, encode, err
from .world import FAULTS, SimClient, SimWorld


class _Handler(socketserver.StreamRequestHandler):
    server: "SimTcpServer"

    def handle(self) -> None:
        with self.server.lock:
            client = SimClient(self.server.world)
        try:
            for raw in self.rfile:
                if not raw.strip():
                    continue
                try:
                    msg = decode(raw)
                except ValueError:
                    self.wfile.write(encode(err(None, 0, "BAD_JSON", "Geçersiz JSON")))
                    continue
                with self.server.lock:
                    out = client.handle(msg)
                for m in out:
                    self.wfile.write(encode(m))
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass
        finally:
            with self.server.lock:
                client.close()


class SimTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int, world: SimWorld):
        super().__init__((host, port), _Handler)
        self.world = world
        self.lock = threading.Lock()


def serve_in_thread(host: str = "127.0.0.1", port: int = 0, world: SimWorld | None = None) -> SimTcpServer:
    srv = SimTcpServer(host, port, world or SimWorld())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Metin2 QA simülatör sunucusu")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=47800)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--password", default="qa")
    ap.add_argument("--fault", action="append", default=[], choices=sorted(FAULTS),
                    help="Bilinçli hata enjekte et (tekrarlanabilir)")
    a = ap.parse_args(argv)
    srv = SimTcpServer(a.host, a.port, SimWorld(a.seed, a.fault, a.password))
    print(f"Metin2 QA sim dinliyor: {a.host}:{srv.server_address[1]} seed={a.seed} faults={a.fault}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
