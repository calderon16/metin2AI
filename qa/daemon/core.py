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
        self.agents.on_owner_whisper = self._on_owner_whisper
        # Sim'de ortak dünya thread-safe değil: işler sırayla
        workers = 1 if self.agents.world is not None else cfg.daemon.max_parallel_jobs
        self.jobs = JobManager(self, workers)
        self.scheduler = Scheduler(self)
        self.lease_timeout_s = 600.0
        self._llm_factory = llm_factory
        self.started = False

    # ------------------------------------------------------------------ LLM
    def _on_owner_whisper(self, account: str, sender: str, text: str) -> None:
        """Sahip fısıldadı: o ajana yüksek öncelikli iş. LLM yoksa ajan kısa bir cevapla bildirir."""
        if not self.llm_available():
            return
        try:
            self.jobs.submit("owner_command", {"account": account, "sender": sender, "text": text},
                             source=f"whisper:{sender}", priority=10)
        except ValueError:
            pass

    def player_info(self) -> dict[str, Any]:
        """Oyuncu modu durumu: ayar, ajan başına şu anki iş ve son oturum özeti (plan)."""
        import json
        from pathlib import Path

        dc = self.cfg.daemon
        try:
            memory = json.loads((Path(self.cfg.resolve("artifacts")) / "daemon" / "player_memory.json")
                                .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            memory = {}
        active: dict[str, dict[str, Any]] = {}
        for j in self.db.list_jobs("queued,running", 200):
            acc = (j.get("params") or {}).get("account")
            if acc and j["type"] in ("play_session", "owner_command") and                     (acc not in active or j["type"] == "owner_command"):
                active[acc] = {"job_id": j["job_id"], "type": j["type"], "status": j["status"],
                               "text": (j.get("params") or {}).get("text")}
        return {"enabled": dc.player_mode, "accounts": dc.player_accounts, "session_steps": dc.player_session_steps,
                "owners": dc.owners, "llm_available": self.llm_available(),
                "agents": {a["account"]: {"activity": active.get(a["account"]),
                                          "last_plan": (memory.get(a["account"]) or {}).get("summary"),
                                          "last_run_id": (memory.get(a["account"]) or {}).get("run_id")}
                           for a in self.agents.list()}}

    def set_player_mode(self, enabled: bool) -> dict[str, Any]:
        """Çalışırken aç/kapat (qa.local.toml'a yazılmaz). Kapatınca sıradaki/çalışan oturumlar iptal edilir."""
        self.cfg.daemon.player_mode = bool(enabled)
        if enabled and self.started and self.scheduler._thread is None:
            self.scheduler.start()
        if not enabled:
            for j in self.db.list_jobs("queued,running", 200):
                if j["type"] == "play_session":
                    self.jobs.cancel(j["job_id"])
        return self.player_info()

    def _paused_accounts(self) -> set[str]:
        import json
        from pathlib import Path

        try:
            mem = json.loads((Path(self.cfg.resolve("artifacts")) / "daemon" / "player_memory.json")
                             .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {acc for acc, e in mem.items() if isinstance(e, dict) and e.get("paused")}

    def owner_command_pending(self, account: str) -> bool:
        return any(j["type"] == "owner_command" and (j.get("params") or {}).get("account") == account
                   for j in self.db.list_jobs("queued", 200))

    def tick_player_mode(self) -> list[int]:
        """Oyuncu modu: boştaki (online) her ajana, kuyrukta başka iş yoksa bir oyun oturumu ver."""
        dc = self.cfg.daemon
        if not dc.player_mode or not self.llm_available():
            return []
        jobs = self.db.list_jobs("queued,running", 200)
        if any(j["status"] == "queued" and j["type"] != "play_session" for j in jobs):
            return []          # önce sıradaki işler (komutlar, testler) ajan alsın
        playing = {(j.get("params") or {}).get("account") for j in jobs
                   if j["type"] in ("play_session", "owner_command")}
        playing |= self._paused_accounts()
        wanted = set(dc.player_accounts)
        created = []
        for a in self.agents.list():
            acc = a["account"]
            if a["status"] != "online" or acc in playing or (wanted and acc not in wanted):
                continue
            try:
                created.append(self.jobs.submit("play_session", {"account": acc}, source="player_mode",
                                                priority=-5))
            except ValueError:
                break
        return created

    def llm_available(self) -> bool:
        if self._llm_factory is not None:
            return True
        if self.cfg.explorer.provider == "ollama":
            from ..planner.llm import ollama_available

            return ollama_available(self.cfg.explorer.ollama_url)
        return bool(os.environ.get(self.cfg.explorer.api_key_env))

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
