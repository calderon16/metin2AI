"""İş kuyruğu: senaryo, suite, affected, keşif, kampanya, replay ve bulgu doğrulama işleri.

İşler SQLite'ta tutulur (daemon yeniden başlasa da geçmiş kalır). Worker thread'leri sıradaki işi
alır, gereken sayıda ajanı kiralar ve mevcut ScenarioRunner / AutoExplorer / replay_run ile çalıştırır.
Gerçek sunucuda işler paralel; sim modunda ortak dünya tek kilitle sırayla kullanılır.
İptal, run'lar arasında (ve ajan beklerken) uygulanır.
"""

from __future__ import annotations

import threading
import traceback
from typing import TYPE_CHECKING, Any, Callable

from ..planner.autonomous import AutoExplorer, ExploreBudget
from ..replay import replay_run
from ..scenario.loader import load_scenario
from ..scenario.runner import ScenarioRunner
from ..store.db import utcnow
from .campaign import run_campaign

if TYPE_CHECKING:  # pragma: no cover
    from .core import Daemon

JOB_TYPES = {
    "scenario": "Tek senaryo. params: name, seed?",
    "suite": "Senaryo grubu. params: tag? (yoksa hepsi), seed?",
    "affected": "Değişen dosyalara göre seçilen senaryolar. params: files? | base?",
    "explore": "Otonom keşif (LLM). params: goal, max_steps?, save_as?, setup?, seed?",
    "campaign": "Her şeyi test et. params: systems?, explore?, seed?, explore_steps?",
    "replay": "Run'ı tekrar oynat. params: run_id, times?",
    "confirm": "Bulguyu replay ile doğrula. params: finding_id, times?",
}


class JobContext:
    def __init__(self, daemon: "Daemon", job: dict[str, Any], cancel_event: threading.Event):
        self.daemon = daemon
        self.job_id = job["job_id"]
        self.params = job["params"] or {}
        self._cancel = cancel_event
        self.run_ids: list[str] = []
        self.agents_used: set[str] = set()
        self._progress: dict[str, Any] = {}

    # ------------------------------------------------------------------ yardımcılar
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise InterruptedError("İş iptal edildi")

    def progress(self, **kw: Any) -> None:
        self._progress.update(kw)
        self.daemon.db.update_job(self.job_id, progress=self._progress)

    def _record(self, run_id: str, agents: list[Any]) -> None:
        self.run_ids.append(run_id)
        self.agents_used |= {a.account for a in agents}
        self.daemon.db.update_job(self.job_id, run_ids=self.run_ids, agents=sorted(self.agents_used))

    def _lease(self, count: int):
        return self.daemon.agents.lease(count, self.job_id, cancelled=self.cancelled,
                                        timeout_s=self.daemon.lease_timeout_s)

    # ------------------------------------------------------------------ çalıştırıcılar
    def run_scenario(self, name: str, seed: int | None = None, system: str | None = None) -> dict[str, Any]:
        d = self.daemon
        sc, text = load_scenario(d.cfg.scenarios_path, name)
        self.check_cancel()
        with self._lease(sc.agent_count) as (agents, factory), d.agents.world_guard():
            runner = ScenarioRunner(d.cfg, d.store, factory)
            accounts = [a.account for a in agents]
            rep = runner.run(sc, text, seed=seed, accounts=accounts)
            self._record(rep["run_id"], agents)
            events = d.findings.ingest(rep, system=d.system_for(name) or system)
            # Yeni/gerilemiş bulguyu hemen aynı ajanlarla doğrula
            if d.cfg.daemon.confirm_findings and any(e["event"] in ("new", "regressed") for e in events):
                try:
                    res = replay_run(runner, rep["run_id"], d.cfg.daemon.confirm_times, accounts=accounts)
                    for e in events:
                        if e["event"] in ("new", "regressed"):
                            d.findings.record_confirmation(e["id"], res)
                    for r in res["runs"]:
                        self._record(r["run_id"], agents)
                except Exception:  # doğrulama hatası run sonucunu bozmasın
                    pass
        return rep

    def explore(self, goal: str, max_steps: int | None = None, save_as: str | None = None,
                setup: list[Any] | None = None, seed: int | None = None, system: str | None = None) -> dict[str, Any]:
        d = self.daemon
        provider = d.make_llm_provider()
        e = d.cfg.explorer
        budget = ExploreBudget(max_steps=max_steps or e.max_steps, max_total_tokens=e.max_total_tokens,
                               history_turns=e.history_turns)
        with self._lease(1) as (agents, factory), d.agents.world_guard():
            out = AutoExplorer(d.cfg, d.store, provider, factory).run(
                goal, budget=budget, account=agents[0].account, seed=seed, setup=setup,
                save_as_scenario=save_as, validate=False).to_dict()
            self._record(out["run_id"], agents)
            from ..scenario.runner import load_run_report

            d.findings.ingest(load_run_report(d.store, out["run_id"]), system=system)
        return out

    def replay(self, run_id: str, times: int = 3) -> dict[str, Any]:
        d = self.daemon
        run = d.store.get_run(run_id)
        if run is None:
            raise KeyError(f"Run yok: {run_id}")
        from ..scenario.loader import parse_scenario
        from pathlib import Path

        sc = parse_scenario((Path(run["artifacts_dir"]) / "scenario.yaml").read_text(encoding="utf-8"))
        with self._lease(sc.agent_count) as (agents, factory), d.agents.world_guard():
            res = replay_run(ScenarioRunner(d.cfg, d.store, factory), run_id, times,
                             accounts=[a.account for a in agents])
            for r in res["runs"]:
                self._record(r["run_id"], agents)
        return res


def execute(ctx: JobContext, job_type: str) -> dict[str, Any]:
    d, p = ctx.daemon, ctx.params
    if job_type == "scenario":
        rep = ctx.run_scenario(p["name"], p.get("seed"))
        return {"run_id": rep["run_id"], "result": rep["result"], "summary": rep["summary"]}
    if job_type in ("suite", "affected"):
        if job_type == "suite":
            names = [s["name"] for s in d.service.list_scenarios(p.get("tag")) if "error" not in s]
            sel = None
        else:
            sel = d.service.select_affected(p.get("files"), p.get("base", "HEAD"))
            names = sel["selected"]
        ctx.progress(total=len(names), done=0)
        results = []
        for i, n in enumerate(names):
            ctx.check_cancel()
            ctx.progress(current=n, done=i)
            rep = ctx.run_scenario(n, p.get("seed"))
            results.append({"scenario": n, "run_id": rep["run_id"], "result": rep["result"], "summary": rep["summary"]})
        ctx.progress(done=len(names), current=None)
        counts: dict[str, int] = {}
        for r in results:
            counts[r["result"]] = counts.get(r["result"], 0) + 1
        out: dict[str, Any] = {"counts": counts, "results": results}
        if sel is not None:
            out["selection"] = {k: sel[k] for k in ("changed_files", "selected", "reasons")}
        return out
    if job_type == "explore":
        return ctx.explore(p["goal"], p.get("max_steps"), p.get("save_as"), p.get("setup"), p.get("seed"))
    if job_type == "campaign":
        return run_campaign(ctx, p.get("systems"), p.get("explore"), p.get("seed"), p.get("explore_steps"))
    if job_type == "replay":
        return ctx.replay(p["run_id"], int(p.get("times", 3)))
    if job_type == "confirm":
        f = d.db.get_finding(int(p["finding_id"]))
        if f is None:
            raise KeyError(f"Bulgu yok: {p['finding_id']}")
        if not f.get("last_run_id") or (d.store.get_run(f["last_run_id"]) or {}).get("mode") == "explore":
            raise ValueError("Bu bulgu keşiften geliyor; doğrulamak için önce senaryo olarak kaydedin")
        res = ctx.replay(f["last_run_id"], int(p.get("times", d.cfg.daemon.confirm_times)))
        return {"status": d.findings.record_confirmation(f["id"], res), "verdict": res["verdict"]}
    raise ValueError(f"Bilinmeyen iş tipi: {job_type}")


class JobManager:
    def __init__(self, daemon: "Daemon", workers: int):
        self.daemon = daemon
        self.workers = max(1, workers)
        self._claim_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel: dict[int, threading.Event] = {}
        self._threads: list[threading.Thread] = []
        self.on_finished: list[Callable[[dict[str, Any]], None]] = []

    def validate(self, job_type: str, params: dict[str, Any]) -> None:
        if job_type not in JOB_TYPES:
            raise ValueError(f"Bilinmeyen iş tipi: {job_type} (mevcut: {sorted(JOB_TYPES)})")
        need = {"scenario": ["name"], "explore": ["goal"], "replay": ["run_id"], "confirm": ["finding_id"]}
        missing = [k for k in need.get(job_type, []) if not params.get(k)]
        if missing:
            raise ValueError(f"{job_type} için eksik parametre: {missing}")
        if job_type == "scenario":
            load_scenario(self.daemon.cfg.scenarios_path, params["name"])  # yoksa hata
        if job_type == "explore" and not self.daemon.llm_available():
            raise ValueError(f"LLM anahtarı yok: {self.daemon.cfg.explorer.api_key_env} ortam değişkenini ayarlayın")
        if job_type == "explore":
            blocked = self.daemon.llm_budget().blocked_reason()
            if blocked:
                raise ValueError(f"{blocked} — keşif yarın (ya da tavan yükseltilince) çalıştırılabilir")

    def submit(self, job_type: str, params: dict[str, Any] | None = None, source: str = "api",
               priority: int = 0) -> int:
        params = params or {}
        self.validate(job_type, params)
        jid = self.daemon.db.create_job(job_type, params, source, priority)
        self._wake.set()
        return jid

    def cancel(self, job_id: int) -> dict[str, Any]:
        job = self.daemon.db.get_job(job_id)
        if job is None:
            raise KeyError(f"İş yok: {job_id}")
        if job["status"] == "queued":
            self.daemon.db.update_job(job_id, status="cancelled", finished_at=utcnow())
        elif job["status"] == "running" and job_id in self._cancel:
            self._cancel[job_id].set()
        return self.daemon.db.get_job(job_id)

    def start(self) -> None:
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"job-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        for ev in self._cancel.values():
            ev.set()
        for t in self._threads:
            t.join(timeout=10)

    def _claim(self) -> dict[str, Any] | None:
        with self._claim_lock:
            job = self.daemon.db.next_queued()
            if job is None:
                return None
            self.daemon.db.update_job(job["job_id"], status="running", started_at=utcnow())
            self._cancel[job["job_id"]] = threading.Event()
            job["status"] = "running"
            return job

    def _worker(self) -> None:
        while not self._stop.is_set():
            job = self._claim()
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            self._run(job)

    def _run(self, job: dict[str, Any]) -> None:
        jid = job["job_id"]
        ctx = JobContext(self.daemon, job, self._cancel[jid])
        try:
            result = execute(ctx, job["type"])
            self.daemon.db.update_job(jid, status="done", finished_at=utcnow(), result=result)
        except InterruptedError as e:
            self.daemon.db.update_job(jid, status="cancelled", finished_at=utcnow(), error=str(e))
        except Exception as e:
            self.daemon.db.update_job(jid, status="failed", finished_at=utcnow(),
                                      error=f"{type(e).__name__}: {e}", result={"traceback": traceback.format_exc()[-3000:]})
        finally:
            self._cancel.pop(jid, None)
            final = self.daemon.db.get_job(jid)
            for cb in self.on_finished:
                try:
                    cb(final)
                except Exception:
                    pass

    def wait(self, job_id: int, timeout_s: float = 60.0) -> dict[str, Any]:
        """Testler/CLI için: iş bitene kadar bekle."""
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            j = self.daemon.db.get_job(job_id)
            if j and j["status"] in ("done", "failed", "cancelled", "interrupted"):
                return j
            time.sleep(0.05)
        raise TimeoutError(f"İş {job_id} {timeout_s} sn içinde bitmedi")
