"""Daemon çekirdeği: ajanlar + iş kuyruğu + zamanlayıcı + bulgular + kampanyalar tek süreçte.

    metin2-qa daemon --config qa.toml
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ..config import QaConfig
from ..planner.llm import LLMProvider
from ..planner.codex_fallback import create_explorer_provider
from ..service import QaService
from .agents import AgentManager
from .campaign import load_catalog
from .db import DaemonDB
from .findings import FindingStore
from .jobs import JobManager
from .scheduler import Scheduler

if TYPE_CHECKING:  # pragma: no cover
    from ..planner.budget import LLMBudget


class Daemon:
    def __init__(self, cfg: QaConfig, llm_factory: Any = None):
        cfg.check_environment()
        self.cfg = cfg
        self.service = QaService(cfg)
        self.store = self.service.store
        self.db = DaemonDB(self.store)
        self.findings = FindingStore(self.db)
        self.agents = AgentManager(cfg)
        # Sim'de ortak dünya thread-safe değil: işler sırayla
        workers = 1 if self.agents.world is not None else cfg.daemon.max_parallel_jobs
        self.jobs = JobManager(self, workers)
        self.scheduler = Scheduler(self)
        self.lease_timeout_s = 600.0
        self._llm_factory = llm_factory
        self.started = False

    # ------------------------------------------------------------------ LLM
    def llm_available(self) -> bool:
        return self._llm_factory is not None or bool(os.environ.get(self.cfg.explorer.api_key_env))

    def make_llm_provider(self) -> LLMProvider:
        if self._llm_factory is not None:
            return self._llm_factory()
        e = self.cfg.explorer
        return create_explorer_provider(e)

    def llm_budget(self) -> "LLMBudget":
        from ..planner.budget import LLMBudget

        return LLMBudget(self.store, self.cfg.explorer)

    def catalog(self) -> list[dict[str, Any]]:
        return load_catalog(self.cfg.resolve(self.cfg.daemon.catalog))

    def system_for(self, scenario: str) -> str | None:
        """Senaryonun asıl sistemi: ilk etiketi hangi katalog sistemine aitse o (yoksa herhangi bir eşleşme)."""
        sc = next((s for s in self.service.list_scenarios() if s["name"] == scenario), None)
        if not sc or not sc.get("tags"):
            return None
        catalog = self.catalog()
        for tag in sc["tags"]:
            for sys in catalog:
                if tag in sys["tags"]:
                    return sys["id"]
        return None

    # ------------------------------------------------------------------ yaşam döngüsü
    def start(self, start_agents: bool = True) -> None:
        self.db.interrupt_running()
        self.agents.start(start_agents)
        self.jobs.start()
        self.scheduler.start()
        self.started = True

    def shutdown(self) -> None:
        self.scheduler.shutdown()
        self.jobs.shutdown()
        self.agents.shutdown()
        self.started = False

    def status(self) -> dict[str, Any]:
        agents = self.agents.list()
        acounts: dict[str, int] = {}
        for a in agents:
            acounts[a["status"]] = acounts.get(a["status"], 0) + 1
        jobs = self.db.list_jobs("queued,running", 100)
        last_campaign = self.db.list_campaigns(1)
        return {
            "env": self.cfg.env,
            "bridge_mode": self.cfg.bridge.mode,
            "agents": acounts,
            "jobs": {"queued": sum(j["status"] == "queued" for j in jobs),
                     "running": sum(j["status"] == "running" for j in jobs)},
            "findings": self.findings.counts(),
            "llm": {"available": self.llm_available(), "provider": self.cfg.explorer.provider,
                    "model": self.cfg.explorer.model, "budget": self.llm_budget().status()},
            "last_campaign": last_campaign[0] if last_campaign else None,
            "workers": self.jobs.workers,
        }
