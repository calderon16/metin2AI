"""Yapılandırmaya göre bridge açan fabrika."""

from __future__ import annotations

from .bridge.client import Bridge, LocalBridge, TcpBridge
from .config import QaConfig
from .sim.world import SimClient, SimWorld


class BridgeFactory:
    """mode=sim: in-process simülatör (world verilmezse her bağlantıya yeni dünya),
    mode=tcp: QA client'a (veya `python -m qa.sim.server`) TCP bağlantısı,
    mode=headless: ekransız paket client (qa/headless) ile doğrudan auth/game sunucusuna."""

    # True ise bridge'ler kalıcı oturumlardır (daemon ajanları): run login/logout/sim_reset yapmaz
    persistent = False

    def __init__(self, cfg: QaConfig, world: SimWorld | None = None):
        self.cfg = cfg
        self.world = world

    @property
    def is_sim(self) -> bool:
        return self.cfg.bridge.mode == "sim"

    def open(self, account: str | None = None, index: int = 0) -> Bridge:
        """account/index: çoklu ajanda her ajan ayrı istemcidir. TCP'de port önce
        [bridge.agent_ports][account]'tan, yoksa `port + index`'ten alınır."""
        b = self.cfg.bridge
        if b.mode == "sim":
            world = self.world or SimWorld(password=self.cfg.accounts.password)
            client = SimClient(world)
            return LocalBridge(client.handle, on_close=client.close)
        if b.mode == "tcp":
            port = b.agent_ports.get(account or "", b.port + index)
            return TcpBridge(b.host, port, b.timeout_s)
        if b.mode == "headless":
            from .headless.client import HeadlessClient

            client = HeadlessClient(self.cfg.headless, self.cfg.resolve)
            return LocalBridge(client.handle, on_close=client.close)
        raise ValueError(f"Bilinmeyen bridge modu: {b.mode}")
