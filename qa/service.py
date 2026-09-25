"""QA servis katmanı: MCP server ve CLI'nin ortak kullandığı işlemler."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .build.manager import BuildManager
from .config import QaConfig, load_config
from .engine import behaviours as _b  # noqa: F401
from .engine.behaviours import login
from .engine.executor import BEHAVIOURS, GameContext
from .engine.rng import QaRandom
from .oracle.assertions import describe_assertions
from .planner.explore import ExplorationManager
from .replay import replay_run
from .scenario.loader import list_scenarios, load_scenario, save_scenario, scenario_path
from .scenario.runner import ScenarioRunner, load_run_report, send_qa_command
from .scenario.schema import SETUP_OPS
from .selection import git_changed_files, select
from .session import BridgeFactory
from .sim.world import FAULTS, SimWorld
from .store.artifacts import read_artifact
from .store.db import Store


def _tail(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if n > 0 else text


class QaService:
    def __init__(self, cfg: QaConfig | None = None):
        self.cfg = cfg or load_config()
        self.cfg.check_environment()
        self.store = Store(self.cfg.db_file)
        self.factory = BridgeFactory(self.cfg)
        self.runner = ScenarioRunner(self.cfg, self.store, self.factory)
        self.explorer = ExplorationManager(self.cfg, self.store, self.factory)
        self.builder = BuildManager(self.cfg)
        # Canlı durum sorguları için kalıcı sim dünyası (yalnızca sim modunda)
        self._live_world = SimWorld(password=self.cfg.accounts.password) if self.factory.is_sim else None

    # ------------------------------------------------------------------ build / sunucu
    def build(self, target: str | None = None) -> dict[str, Any]:
        return self.builder.build(target)

    def start_test_server(self) -> dict[str, Any]:
        if self.factory.is_sim and not self.cfg.server.start:
            return {"ok": True, "note": "Sim modu: sunucu in-process, başlatmaya gerek yok"}
        return self.builder.start_server()

    def stop_test_server(self) -> dict[str, Any]:
        if self.factory.is_sim and not self.cfg.server.stop:
            return {"ok": True, "note": "Sim modu: durdurulacak sunucu yok"}
        return self.builder.stop_server()

    # ------------------------------------------------------------------ senaryolar
    def list_scenarios(self, tag: str | None = None) -> list[dict[str, Any]]:
        out = list_scenarios(self.cfg.scenarios_path)
        return [s for s in out if tag is None or tag in s.get("tags", [])]

    def get_scenario(self, name: str) -> str:
        return scenario_path(self.cfg.scenarios_path, name).read_text(encoding="utf-8")

    def write_scenario(self, name: str, yaml_text: str, overwrite: bool = False) -> dict[str, Any]:
        p = save_scenario(self.cfg.scenarios_path, name, yaml_text, overwrite)
        return {"ok": True, "path": str(p)}

    def reference(self) -> dict[str, Any]:
        return {
            "behaviours": [b.describe() for b in BEHAVIOURS.values()],
            "assertions": describe_assertions(),
            "setup_ops": SETUP_OPS,
            "step_meta": {"expect": "satır içi assert listesi", "expect_error": "beklenen red kodu",
                          "label": "etiket", "continue_on_failure": "hata olsa da devam et"},
            "sim_faults": FAULTS if self.factory.is_sim else None,
        }

    # ------------------------------------------------------------------ çalıştırma
    def run_scenario(self, name: str, seed: int | None = None) -> dict[str, Any]:
        sc, text = load_scenario(self.cfg.scenarios_path, name)
        return self.runner.run(sc, text, seed=seed)

    def run_suite(self, tag: str | None = None, seed: int | None = None) -> dict[str, Any]:
        results = []
        for s in self.list_scenarios(tag):
            if "error" in s:
                results.append({"scenario": s["name"], "result": "ERROR", "summary": s["error"]})
                continue
            rep = self.run_scenario(s["name"], seed)
            results.append({"scenario": s["name"], "run_id": rep["run_id"], "result": rep["result"],
                            "summary": rep["summary"]})
        counts: dict[str, int] = {}
        for r in results:
            counts[r["result"]] = counts.get(r["result"], 0) + 1
        return {"ok": all(r["result"] == "PASSED" for r in results), "counts": counts, "results": results}

    def select_affected(self, changed_files: list[str] | None = None, base: str = "HEAD") -> dict[str, Any]:
        if changed_files is None:
            changed_files = git_changed_files(self.cfg.resolve(self.cfg.source_repo), base)
        return select(self.cfg, changed_files, self.list_scenarios())

    def run_affected(self, changed_files: list[str] | None = None, base: str = "HEAD",
                     seed: int | None = None, dry_run: bool = False) -> dict[str, Any]:
        sel = self.select_affected(changed_files, base)
        if dry_run or not sel["selected"]:
            return {"ok": True, "ran": False, **sel}
        results = []
        for name in sel["selected"]:
            rep = self.run_scenario(name, seed)
            results.append({"scenario": name, "run_id": rep["run_id"], "result": rep["result"],
                            "summary": rep["summary"]})
        return {"ok": all(r["result"] == "PASSED" for r in results), "ran": True, **sel, "results": results}

    def explore_auto(self, goal: str, max_steps: int | None = None, save_as_scenario: str | None = None,
                     setup: list[Any] | None = None, seed: int | None = None, account: str | None = None,
                     model: str | None = None, provider: Any = None, validate: bool = True) -> dict[str, Any]:
        """Otonom keşif: LLM (varsayılan Gemini) hedefe göre oyunu kendi başına test eder."""
        from .planner.autonomous import AutoExplorer, ExploreBudget
        from .planner.codex_fallback import create_explorer_provider

        e = self.cfg.explorer
        if provider is None:
            provider = create_explorer_provider(e, model)
        budget = ExploreBudget(max_steps=max_steps or e.max_steps, max_total_tokens=e.max_total_tokens,
                               history_turns=e.history_turns)
        out = AutoExplorer(self.cfg, self.store, provider, self.factory).run(
            goal, budget=budget, account=account, seed=seed, setup=setup, save_as_scenario=save_as_scenario,
            validate=validate)
        return out.to_dict()

    def llm_usage(self, days: int = 30) -> dict[str, Any]:
        """LLM bütçe durumu (bugün/bu ay, tavanlar) ve günlük kullanım geçmişi."""
        from .planner.budget import LLMBudget

        b = LLMBudget(self.store, self.cfg.explorer)
        return {**b.status(), "daily": b.daily(days)}

    def replay_failure(self, run_id: str, times: int = 3) -> dict[str, Any]:
        return replay_run(self.runner, run_id, times)

    # ------------------------------------------------------------------ sonuçlar
    def get_test_runs(self, limit: int = 20, status: str | None = None,
                      scenario: str | None = None) -> list[dict[str, Any]]:
        keep = ("run_id", "scenario", "mode", "status", "seed", "git_commit", "branch", "started_at",
                "replay_of", "summary")
        return [{k: r[k] for k in keep} for r in self.store.list_runs(limit, status, scenario)]

    def get_test_result(self, run_id: str) -> dict[str, Any]:
        return load_run_report(self.store, run_id)

    def get_failed_tests(self, limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for r in self.store.list_runs(limit, status="FAILED") + self.store.list_runs(limit, status="ERROR"):
            out.append({"run_id": r["run_id"], "scenario": r["scenario"], "status": r["status"], "seed": r["seed"],
                        "git_commit": r["git_commit"], "summary": r["summary"],
                        "failures": self.store.failures(r["run_id"])})
        out.sort(key=lambda r: r["run_id"], reverse=True)
        return out[:limit]

    def _run_dir(self, run_id: str) -> Path:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"Run bulunamadı: {run_id}")
        return Path(run["artifacts_dir"])

    def read_text_artifact(self, run_id: str, name: str, tail: int = 200) -> str:
        try:
            p = read_artifact(self._run_dir(run_id), name)
        except FileNotFoundError:
            return f"({name} yok)"
        return _tail(p.read_text(encoding="utf-8", errors="replace"), tail)

    def get_trace(self, run_id: str, tail: int = 200) -> str:
        return self.read_text_artifact(run_id, "trace.txt", tail)

    def get_server_logs(self, run_id: str, tail: int = 200) -> str:
        d = self._run_dir(run_id)
        parts = [f"== server.log (QA olayları) ==\n{self.read_text_artifact(run_id, 'server.log', tail)}"]
        for p in sorted(d.glob("server_*.log")):
            parts.append(f"== {p.name} ==\n{_tail(p.read_text(encoding='utf-8', errors='replace'), tail)}")
        return "\n\n".join(parts)

    def get_client_logs(self, run_id: str, tail: int = 200) -> str:
        return self.read_text_artifact(run_id, "client.log", tail)

    def screenshot_path(self, run_id: str, name: str | None = None) -> Path:
        d = self._run_dir(run_id)
        if name:
            return read_artifact(d, name if name.startswith("screenshots/") else f"screenshots/{name}")
        shots = sorted(p for p in (d / "screenshots").iterdir() if p.is_file()) if (d / "screenshots").exists() else []
        if not shots:
            raise FileNotFoundError(f"{run_id} için ekran görüntüsü yok")
        return shots[-1]

    # ------------------------------------------------------------------ canlı
    def _live_ctx(self, account: str | None) -> GameContext:
        account = account or self.cfg.accounts.default_account
        self.cfg.check_account(account)
        f = BridgeFactory(self.cfg, self._live_world) if self._live_world else self.factory
        bridge = f.open()
        bridge.connect()
        ctx = GameContext(bridge, QaRandom(0), account=account, password=self.cfg.accounts.password)
        if not ctx.state().get("in_game"):
            login(ctx)
        return ctx

    def get_player_state(self, account: str | None = None) -> dict[str, Any]:
        ctx = self._live_ctx(account)
        try:
            return {"player": ctx.state(), "inventory": ctx.inventory(), "target": ctx.query("get_target"),
                    "nearby_entities": ctx.entities(radius=5000)[:30], "quest_state": ctx.query("get_quest_state"),
                    "ui": ctx.windows(), "system_messages": ctx.query("get_system_messages", since=0)[-10:]}
        finally:
            ctx.bridge.close()

    def reset_test_account(self, account: str) -> dict[str, Any]:
        ctx = self._live_ctx(account)
        try:
            reply = send_qa_command(ctx, "/qa reset")
            return {"ok": True, "account": account, "reply": reply, "state": ctx.state()}
        finally:
            ctx.bridge.close()

    def dumps(self, data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, indent=1, default=str)
