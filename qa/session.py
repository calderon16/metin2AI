"""Yapılandırmaya göre bridge açan fabrika."""

from __future__ import annotations

from .bridge.client import Bridge, LocalBridge, TcpBridge
from .config import QaConfig
from .sim.world import SimClient, SimWorld


class BridgeFactory:
    """mode=sim: in-process simülatör (world verilmezse her bağlantıya yeni dünya),
    mode=tcp: QA client'a (veya `python -m qa.sim.server`) TCP bağlantısı."""

    def __init__(self, cfg: QaConfig, world: SimWorld | None = None):
        self.cfg = cfg
        self.world = world

    @property
    def is_sim(self) -> bool:
        return self.cfg.bridge.mode == "sim"

    def open(self) -> Bridge:
        b = self.cfg.bridge
        if b.mode == "sim":
            world = self.world or SimWorld(password=self.cfg.accounts.password)
            client = SimClient(world)
            return LocalBridge(client.handle, on_close=client.close)
        if b.mode == "tcp":
            return TcpBridge(b.host, b.port, b.timeout_s)
        raise ValueError(f"Bilinmeyen bridge modu: {b.mode}")
