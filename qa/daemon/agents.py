"""Ajan yöneticisi: 7/24 oyunda duran AI oyuncular.

Her ajan bir QA hesabıdır ve kalıcı bir bridge'e sahiptir (sim, headless paket client ya da eski
QA client). Ajan işler arasında oyunda kalır ("online tut"); bağlantı koparsa geri çekilmeli
(backoff) yeniden bağlanır. İşler ajanları kiralar (lease); kiralık ajanın bridge'i RunSession'a
`close()`'u etkisiz bir sarmalayıcıyla verilir, böylece run bitince ajan oyundan çıkmaz.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..bridge.client import Bridge
from ..bridge.protocol import ActionError, BridgeError
from ..config import QaConfig
from ..engine.behaviours import login
from ..engine.executor import BehaviourError, GameContext
from ..engine.rng import QaRandom
from ..session import BridgeFactory
from ..sim.world import SimWorld
from ..store.db import utcnow

# stopped: elle durduruldu | connecting | online (boşta) | busy (işte) | offline (koptu, yeniden denenecek) | error
STATUSES = ("stopped", "connecting", "online", "busy", "offline", "error")


@dataclass
class AgentState:
    account: str
    character: str
    keep_online: bool = True
    tags: list[str] = field(default_factory=list)
    status: str = "stopped"
    enabled: bool = False                       # başlatıldı mı (panelden başlat/durdur)
    bridge: Bridge | None = None
    job_id: int | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    connected_at: str | None = None
    reconnects: int = 0
    next_retry: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def public(self) -> dict[str, Any]:
        return {"account": self.account, "character": self.character, "keep_online": self.keep_online,
                "tags": self.tags, "status": self.status, "enabled": self.enabled, "job_id": self.job_id,
                "snapshot": self.snapshot, "last_error": self.last_error, "connected_at": self.connected_at,
                "reconnects": self.reconnects}


class LeasedBridge:
    """Kiralanmış kalıcı bridge: run'ın `close()` çağrısı ajanı oyundan çıkarmaz."""

    def __init__(self, inner: Bridge):
        self._inner = inner

    def close(self) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class LeaseFactory(BridgeFactory):
    """RunSession'a kiralanmış ajanların bridge'lerini veren fabrika."""

    persistent = True

    def __init__(self, cfg: QaConfig, world: SimWorld | None, agents: list[AgentState]):
        super().__init__(cfg, world)
        self._by_account = {a.account: a for a in agents}

    def open(self, account: str | None = None, index: int = 0) -> Bridge:
        a = self._by_account.get(account or "")
        if a is None or a.bridge is None:
            raise BridgeError(f"{account} bu işe kiralanmadı veya bağlı değil")
        return LeasedBridge(a.bridge)  # type: ignore[return-value]


class AgentManager:
    def __init__(self, cfg: QaConfig, world: SimWorld | None = None):
        self.cfg = cfg
        self.dcfg = cfg.daemon
        # Sim modunda tüm ajanlar tek ortak dünyada; dünya thread-safe olmadığından erişim tek kilitle
        self.world = world if world is not None else (
            SimWorld(password=cfg.accounts.password, faults=cfg.sim.faults) if cfg.bridge.mode == "sim" else None)
        self.world_lock = threading.RLock() if self.world is not None else None
        self.raw_factory = BridgeFactory(cfg, self.world)
        self.agents: dict[str, AgentState] = {}
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        for spec in self.dcfg.agents:
            self.add(spec.get("account"), spec.get("character"), spec.get("keep_online", True),
                     spec.get("tags", []), start=False)

    # ------------------------------------------------------------------ yaşam döngüsü
    def world_guard(self) -> contextlib.AbstractContextManager:
        return self.world_lock if self.world_lock is not None else contextlib.nullcontext()

    def start(self, start_agents: bool = True) -> None:
        if start_agents:
            for a in self.agents.values():
                if a.keep_online:
                    a.enabled = True
                    a.status = "connecting"
        self._thread = threading.Thread(target=self._monitor, name="agent-monitor", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        for a in self.agents.values():
            self._disconnect(a, logout=True)

    def add(self, account: str | None, character: str | None = None, keep_online: bool = True,
            tags: list[str] | None = None, start: bool = True) -> AgentState:
        if not account:
            raise ValueError("account gerekli")
        self.cfg.check_account(account)
        character = character or account
        self.cfg.check_account(character)
        if account in self.agents:
            raise ValueError(f"{account} zaten ekli")
        a = AgentState(account, character, keep_online, list(tags or []))
        self.agents[account] = a
        if start:
            self.enable(account)
        return a

    def remove(self, account: str) -> None:
        a = self._get(account)
        if a.status == "busy":
            raise ValueError(f"{account} bir işte, önce işi iptal edin")
        self.disable(account)
        del self.agents[account]

    def _get(self, account: str) -> AgentState:
        if account not in self.agents:
            raise KeyError(f"Ajan yok: {account}")
        return self.agents[account]

    def enable(self, account: str) -> None:
        a = self._get(account)
        a.enabled = True
        if a.status in ("stopped", "error", "offline"):
            a.status, a.next_retry = "connecting", 0.0

    def disable(self, account: str) -> None:
        a = self._get(account)
        a.enabled = False
        if a.status != "busy":
            with a.lock:
                self._disconnect(a, logout=True)
                a.status = "stopped"

    def reconnect(self, account: str) -> None:
        a = self._get(account)
        if a.status == "busy":
            raise ValueError(f"{account} bir işte")
        with a.lock:
            self._disconnect(a, logout=True)
            a.enabled, a.status, a.next_retry = True, "connecting", 0.0

    # ------------------------------------------------------------------ bağlantı
    def _connect(self, a: AgentState) -> None:
        with self.world_guard():
            bridge = self.raw_factory.open(a.account)
            try:
                bridge.connect()
                ctx = GameContext(bridge, QaRandom(0), account=a.account, password=self.cfg.accounts.password,
                                  character=a.character)
                if not ctx.state().get("in_game"):
                    login(ctx)
                bridge.drain_events()
            except BaseException:
                bridge.close()
                raise
        a.bridge = bridge
        a.status, a.last_error, a.connected_at = "online", None, utcnow()
        self._refresh(a)

    def _disconnect(self, a: AgentState, logout: bool) -> None:
        if a.bridge is None:
            return
        with self.world_guard():
            if logout:
                with contextlib.suppress(ActionError, BridgeError):
                    a.bridge.call("logout")
            with contextlib.suppress(Exception):
                a.bridge.close()
        a.bridge = None

    def _refresh(self, a: AgentState) -> None:
        with self.world_guard():
            st = a.bridge.call("get_player_state")
            a.bridge.drain_events()
        a.snapshot = {k: st.get(k) for k in ("name", "level", "hp", "max_hp", "gold", "map", "x", "y", "channel",
                                              "dead", "in_game")}
        a.snapshot["updated_at"] = utcnow()
        if not st.get("in_game"):
            raise BridgeError("Karakter oyundan düştü")

    def _backoff(self, a: AgentState) -> float:
        steps = self.dcfg.reconnect_backoff_s or [5]
        return steps[min(a.reconnects, len(steps) - 1)]

    def _monitor(self) -> None:
        last_snap: dict[str, float] = {}
        while not self._stop.is_set():
            now = time.monotonic()
            # Sim'de bir iş dünyayı kullanıyorsa bu turu atla (iş thread'i dünya kilidini tutar)
            if self.world_lock is not None and not self.world_lock.acquire(blocking=False):
                self._stop.wait(0.5)
                continue
            try:
                self._monitor_tick(now, last_snap)
            finally:
                if self.world_lock is not None:
                    self.world_lock.release()
            self._stop.wait(0.5)

    def _monitor_tick(self, now: float, last_snap: dict[str, float]) -> None:
        for a in list(self.agents.values()):
            if not a.enabled or a.status == "busy":
                continue
            if not a.lock.acquire(blocking=False):
                continue
            try:
                if a.status in ("connecting", "offline", "error") and now >= a.next_retry:
                    try:
                        self._connect(a)
                        last_snap[a.account] = now
                        with self._cond:
                            self._cond.notify_all()
                    except (BridgeError, ActionError, BehaviourError, OSError) as e:
                        a.reconnects += 1
                        a.last_error = f"Bağlanamadı: {e}"
                        a.status = "offline"
                        a.next_retry = now + self._backoff(a)
                elif a.status == "online" and now - last_snap.get(a.account, 0) >= self.dcfg.snapshot_interval_s:
                    last_snap[a.account] = now
                    try:
                        self._refresh(a)
                    except (BridgeError, ActionError, OSError) as e:
                        self._disconnect(a, logout=False)
                        a.reconnects += 1
                        a.last_error = f"Bağlantı koptu: {e}"
                        a.status = "offline" if a.keep_online else "stopped"
                        a.next_retry = now + self._backoff(a)
            finally:
                a.lock.release()

    # ------------------------------------------------------------------ kiralama
    def acquire(self, count: int, job_id: int, timeout_s: float = 600.0, cancelled: Any = None,
                prefer: list[str] | None = None) -> list[AgentState]:
        """`count` boş (online) ajanı kirala; yoksa bekle. prefer: tercih edilen hesaplar."""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                if cancelled is not None and cancelled():
                    raise InterruptedError("İş iptal edildi")
                free = [a for a in self.agents.values() if a.status == "online" and a.enabled]
                if prefer:
                    free.sort(key=lambda a: (a.account not in prefer, a.account))
                else:
                    free.sort(key=lambda a: a.account)
                if len(free) >= count:
                    # Kilitleri bloklamadan al: izleme thread'i bir ajanı yeniliyor olabilir (kilit sırası
                    # tersine dönüp kilitlenmesin diye beklemek yerine kısa süre sonra yeniden dene)
                    got: list[AgentState] = []
                    for a in free:
                        if len(got) == count:
                            break
                        if a.lock.acquire(blocking=False):
                            got.append(a)
                    if len(got) == count:
                        for a in got:
                            a.status, a.job_id = "busy", job_id
                        return got
                    for a in got:
                        a.lock.release()
                    self._cond.wait(timeout=0.1)
                    continue
                if not any(a.enabled for a in self.agents.values()):
                    raise RuntimeError("Hiç başlatılmış ajan yok")
                if len([a for a in self.agents.values() if a.enabled]) < count:
                    raise RuntimeError(f"Bu iş {count} ajan istiyor, yalnızca "
                                       f"{len([a for a in self.agents.values() if a.enabled])} ajan başlatılmış")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{count} boş ajan {timeout_s:.0f} sn içinde bulunamadı")
                self._cond.wait(timeout=1.0)

    def release(self, agents: list[AgentState]) -> None:
        with self._cond:
            for a in agents:
                a.job_id = None
                if a.bridge is None:
                    a.status = "offline" if a.enabled else "stopped"
                else:
                    try:
                        self._refresh(a)
                        a.status = "online" if a.enabled else "stopped"
                    except (BridgeError, ActionError, OSError) as e:
                        self._disconnect(a, logout=False)
                        a.last_error = f"İş sonrası bağlantı yok: {e}"
                        a.status = "offline" if a.enabled else "stopped"
                    if not a.enabled:
                        self._disconnect(a, logout=True)
                with contextlib.suppress(RuntimeError):
                    a.lock.release()
            self._cond.notify_all()

    @contextlib.contextmanager
    def lease(self, count: int, job_id: int, **kw: Any) -> Iterator[tuple[list[AgentState], LeaseFactory]]:
        agents = self.acquire(count, job_id, **kw)
        try:
            yield agents, LeaseFactory(self.cfg, self.world, agents)
        finally:
            self.release(agents)

    def list(self) -> list[dict[str, Any]]:
        return [a.public() for a in self.agents.values()]

    def wait_online(self, count: int | None = None, timeout_s: float = 30.0) -> bool:
        """Testler/başlangıç için: istenen sayıda ajan online olana kadar bekle."""
        need = count if count is not None else len([a for a in self.agents.values() if a.enabled])
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if len([a for a in self.agents.values() if a.status in ("online", "busy")]) >= need:
                return True
            time.sleep(0.05)
        return False
