"""Senaryo çalıştırıcı.

Akış: bağlan → (sim ise dünyayı seed ile sıfırla) → login → karakter seç → /qa reset + setup →
başlangıç görüntüsü → adımlar (+ satır içi expect) → final assert → örtük oracle kontrolleri
(SYSERR yok, QA_ASSERT ihlali yok) → kanıt topla → rapor.

RunSession hem sabit senaryolar hem de Claude'un adım adım yönettiği keşif (explore) modu
tarafından kullanılır.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bridge.client import Bridge
from ..bridge.protocol import ActionError, BridgeError
from ..config import QaConfig
from ..engine.behaviours import login
from ..engine.executor import BehaviourError, GameContext, execute_step
from ..engine.rng import QaRandom, new_seed
from ..oracle.assertions import AssertionResult, Observation, evaluate, take_snapshot
from ..oracle.signals import BridgeEventSource, EventSource, FileEventSource, LogFileSource, ServerSignals
from ..session import BridgeFactory
from ..store.artifacts import RunArtifacts
from ..store.db import Store, utcnow
from ..store.report import git_info, summarize
from .schema import AssertSpec, OracleOptions, Scenario, SetupOp, Step

QA_REPLY_TIMEOUT_MS = 5000


class SetupError(Exception):
    pass


def _jsonable(x: Any, limit: int = 2000) -> Any:
    try:
        s = json.dumps(x, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(x)[:limit]
    return x if len(s) <= limit else s[:limit] + "…"


def send_qa_command(ctx: GameContext, command: str) -> str:
    """`/qa ...` hazırlık komutunu gönder ve sunucunun [QA] OK/ERR yanıtını bekle."""
    msgs = ctx.query("get_system_messages", since=0)
    cursor = msgs[-1]["seq"] if msgs else 0
    ctx.act("send_chat", message=command)
    start = ctx.now()
    while True:
        for m in ctx.query("get_system_messages", since=cursor):
            cursor = m["seq"]
            if m["text"].startswith("[QA] OK"):
                return m["text"]
            if m["text"].startswith("[QA] ERR"):
                raise SetupError(f"{command}: {m['text']}")
        if ctx.now() - start > QA_REPLY_TIMEOUT_MS:
            raise SetupError(f"{command}: sunucu yanıt vermedi (QA komutları kurulu mu?)")
        ctx.wait(100)


class RunSession:
    def __init__(self, cfg: QaConfig, store: Store, factory: BridgeFactory, *, scenario: str, mode: str,
                 seed: int | None, account: str | None = None, character: str | None = None,
                 scenario_text: str | None = None, faults: list[str] | None = None,
                 replay_of: str | None = None):
        cfg.check_environment()
        self.cfg, self.store, self.factory = cfg, store, factory
        self.scenario, self.mode = scenario, mode
        self.seed = seed if seed is not None else (cfg.default_seed if cfg.default_seed is not None else new_seed())
        self.account = account or cfg.accounts.default_account
        cfg.check_account(self.account)
        self.character = character or self.account
        cfg.check_account(self.character)
        self.faults = list(dict.fromkeys((faults or []) + cfg.sim.faults))
        self.scenario_text = scenario_text
        self.build = git_info(cfg.resolve(cfg.source_repo))
        self.run_id, adir = store.new_run(scenario, mode, self.seed, self.account, self.build["git_commit"],
                                          self.build["branch"], cfg.artifacts_path, replay_of)
        self.replay_of = replay_of
        self.artifacts = RunArtifacts(adir)
        if scenario_text:
            self.artifacts.write_text("scenario.yaml", scenario_text)
        self.started_at = utcnow()
        self.bridge: Bridge | None = None
        self.ctx: GameContext | None = None
        self.signals: ServerSignals | None = None
        self.baseline: dict[str, Any] | None = None
        self.steps: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self.error: str | None = None
        self._t0 = 0
        self.finished = False
        self.report: dict[str, Any] | None = None

    # ------------------------------------------------------------------ başlangıç
    def start(self) -> None:
        """Bağlan, giriş yap. Hata durumunda failure/error kaydeder ve False durumuna geçer."""
        self.bridge = self.factory.open()
        info = self.bridge.connect()
        caps = set(info.get("capabilities", []))
        if "sim_control" in caps:
            self.bridge.call("sim_reset", seed=self.seed, faults=self.faults)
        rng = QaRandom(self.seed)
        self.ctx = GameContext(self.bridge, rng, account=self.account, password=self.cfg.accounts.password,
                               character=self.character, screenshot_sink=self.artifacts.save_screenshot)
        self.ctx.note("run_start", run_id=self.run_id, scenario=self.scenario, seed=self.seed)
        self.signals = ServerSignals(self._event_sources(caps), character=self.character)
        self.signals.mark()
        self._t0 = self.ctx.now()
        self.ctx.step = 0
        login(self.ctx)

    def _event_sources(self, caps: set[str]) -> list[EventSource]:
        sc = self.cfg.server
        sources: list[EventSource] = []
        if sc.events_file:
            sources.append(FileEventSource(self.cfg.resolve(sc.events_file)))
        elif "server_events" in caps:
            sources.append(BridgeEventSource(self.bridge))
        for name, path in sc.log_files.items():
            sources.append(LogFileSource(name, self.cfg.resolve(path), sc.error_patterns))
        return sources

    def qa_command(self, command: str) -> str:
        return send_qa_command(self.ctx, command)

    def setup(self, ops: list[SetupOp], reset: bool = True) -> None:
        self.ctx.step = 0
        if reset:
            self.qa_command("/qa reset")
        for op in ops:
            self.qa_command(op.to_command())
        self.ctx.wait(300)
        self.baseline = take_snapshot(self.ctx)
        self.signals.poll()
        self.ctx.note("baseline", state=self.baseline["state"])

    # ------------------------------------------------------------------ adımlar
    def run_step(self, step: Step, index: int) -> list[dict[str, Any]]:
        ctx = self.ctx
        ctx.step = index
        rec: dict[str, Any] = {"index": index, "name": step.name, "args": step.args, "label": step.label,
                               "t_start": ctx.now()}
        ctx.note("step_start", name=step.name, args=step.args, label=step.label)
        fails: list[dict[str, Any]] = []
        try:
            result = execute_step(ctx, step.name, step.args)
            rec["result"] = _jsonable(result)
            if step.expect_error:
                fails.append({"step": index, "action": step.name, "label": step.label, "kind": "assertion",
                              "name": "expect_error", "expected": {"error": step.expect_error},
                              "actual": {"error": None, "result": rec["result"]},
                              "message": f"Adımın {step.expect_error} ile reddedilmesi bekleniyordu ama başarılı oldu"})
        except BehaviourError as e:
            got = {e.code, e.details.get("reject_code")}
            if step.expect_error and step.expect_error in got:
                rec["result"] = {"rejected": step.expect_error}
            else:
                rec["error"] = f"{e.code}: {e.message}"
                fails.append({"step": index, "action": step.name, "label": step.label, "kind": "action_error",
                              "name": e.code,
                              "expected": {"error": step.expect_error} if step.expect_error else {"step_succeeds": True},
                              "actual": {"error": e.code, "details": _jsonable(e.details, 1000)} if e.details
                              else {"error": e.code},
                              "message": e.message})
        if not fails and step.expect:
            fails += self.check(step.expect, index, step.name)
        rec["t_end"] = ctx.now()
        rec["status"] = "failed" if fails else "passed"
        ctx.note("step_end", name=step.name, status=rec["status"])
        self.steps.append(rec)
        if fails:
            self._failure_screenshot(index)
        self.failures += fails
        return fails

    def check(self, specs: list[AssertSpec], step: int | None = None, action: str | None = None) -> list[dict[str, Any]]:
        obs = Observation(self.ctx, self.baseline or take_snapshot(self.ctx), self.signals)
        fails = []
        for spec in specs:
            r = evaluate(obs, spec.name, spec.args)
            self._record_assertion(r, step)
            if not r.passed:
                fails.append(self._assert_failure(r, step, action))
        return fails

    def implicit_checks(self, opts: OracleOptions) -> list[dict[str, Any]]:
        specs = []
        if not opts.allow_server_errors:
            specs.append(AssertSpec(name="server_errors", args={"equals": 0}))
        if not opts.allow_qa_asserts:
            specs.append(AssertSpec(name="qa_asserts", args={"equals": 0}))
        fails = self.check(specs)
        for f in fails:
            f["kind"] = "oracle"
        self.failures += fails
        return fails

    def _record_assertion(self, r: AssertionResult, step: int | None) -> None:
        d = r.to_dict()
        d["step"] = step
        d["actual"] = _jsonable(d["actual"])
        self.assertions.append(d)
        self.ctx.note("assert", name=r.name, passed=r.passed)

    @staticmethod
    def _assert_failure(r: AssertionResult, step: int | None, action: str | None) -> dict[str, Any]:
        return {"step": step, "action": action, "kind": "assertion", "name": r.name, "params": r.params,
                "expected": r.expected, "actual": _jsonable(r.actual), "message": r.message or "ASSERTION FAILED"}

    def _failure_screenshot(self, index: int) -> None:
        try:
            self.ctx.screenshot(f"failure_step_{index}")
        except (ActionError, BridgeError, OSError):
            pass

    def elapsed(self) -> int:
        return self.ctx.now() - self._t0 if self.ctx else 0

    # ------------------------------------------------------------------ bitiş
    def fail_infra(self, message: str) -> None:
        self.error = message

    def finish(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.finished:
            return self.report
        self.finished = True
        ctx = self.ctx
        client_log = ""
        if ctx is not None and self.bridge is not None:
            try:
                if ctx.state().get("in_game"):
                    ctx.step = None
                    ctx.act("logout")
            except (ActionError, BridgeError):
                pass
            try:
                client_log = "\n".join(f"[{m['t']}] {m['text']}" for m in ctx.query("get_client_log", since=0))
            except (ActionError, BridgeError):
                client_log = "(istemci log komutunu desteklemiyor)"
            try:
                if self.signals:
                    self.signals.poll()
            except (ActionError, BridgeError, OSError):
                pass
        if self.bridge is not None:
            self.bridge.close()

        if self.error:
            result = "ERROR"
        elif self.failures:
            result = "FAILED"
        else:
            result = "PASSED"

        a = self.artifacts
        evidence: dict[str, Any] = {"report": "report.json"}
        if ctx is not None:
            ctx.trace.write_jsonl(a.path("trace.jsonl"))
            evidence["action_trace"] = "trace.jsonl"
            evidence["action_trace_text"] = a.write_text("trace.txt", ctx.trace.format_text())
        evidence["client_log"] = a.write_text("client.log", client_log)
        server_log = ""
        if self.signals:
            a.write_text("server_events.jsonl", "\n".join(json.dumps(e, ensure_ascii=False)
                                                           for e in self.signals.events))
            evidence["server_events"] = "server_events.jsonl"
            server_log = self.signals.format_events()
            for name, lines in self.signals.log_lines().items():
                evidence[f"server_log_{name}"] = a.write_text(f"server_{name}.log", "\n".join(lines))
        evidence["server_log"] = a.write_text("server.log", server_log)
        if self.scenario_text:
            evidence["scenario"] = "scenario.yaml"
        evidence["screenshots"] = a.screenshots()

        report: dict[str, Any] = {
            "run_id": self.run_id,
            "scenario": self.scenario,
            "mode": self.mode,
            "result": result,
            "seed": self.seed,
            "player": self.character,
            "account": self.account,
            "replay_of": self.replay_of,
            "build": {**self.build, "env": self.cfg.env},
            "client": self.bridge.info if self.bridge else None,
            "sim_faults": self.faults if self.factory.is_sim else None,
            "started_at": self.started_at,
            "finished_at": utcnow(),
            "game_time_ms": self.elapsed(),
            "error": self.error,
            "failure": self.failures[0] if self.failures else None,
            "failures": self.failures,
            "steps": self.steps,
            "assertions": self.assertions,
            "server": {
                "errors": len(self.signals.errors) if self.signals else None,
                "qa_assert_failures": len(self.signals.assert_failures) if self.signals else None,
                "events": len(self.signals.events) if self.signals else None,
            },
            "trace_digest": ctx.trace.digest() if ctx else None,
            "evidence": evidence,
        }
        if extra:
            report.update(extra)
        report["summary"] = summarize(report) + (f" — {self.error}" if self.error else "")
        a.write_json("report.json", report)
        self.store.finish_run(self.run_id, result, report["summary"], report["game_time_ms"],
                              report["trace_digest"], self.failures)
        self.report = report
        return report


class ScenarioRunner:
    def __init__(self, cfg: QaConfig, store: Store, factory: BridgeFactory | None = None):
        self.cfg, self.store = cfg, store
        self.factory = factory or BridgeFactory(cfg)

    def run(self, sc: Scenario, scenario_text: str | None = None, seed: int | None = None,
            replay_of: str | None = None, mode: str = "scenario") -> dict[str, Any]:
        s = RunSession(self.cfg, self.store, self.factory, scenario=sc.name, mode=mode,
                       seed=seed if seed is not None else sc.seed, account=sc.account, character=sc.character,
                       scenario_text=scenario_text, faults=sc.sim_faults, replay_of=replay_of)
        try:
            try:
                s.start()
            except BehaviourError as e:
                s.failures.append({"step": 0, "action": "login", "kind": "action_error", "name": e.code,
                                   "expected": {"in_game": True}, "actual": {"error": e.code}, "message": e.message})
                return s.finish()
            except ActionError as e:
                s.failures.append({"step": 0, "action": e.cmd, "kind": "action_error", "name": e.code,
                                   "expected": {"in_game": True}, "actual": {"error": e.code}, "message": e.message})
                return s.finish()
            try:
                s.setup(sc.setup, reset=sc.reset)
            except (SetupError, ActionError) as e:
                s.fail_infra(f"Setup başarısız: {e}")
                return s.finish()
            for i, step in enumerate(sc.steps, start=1):
                if s.elapsed() > sc.timeout_ms:
                    s.failures.append({"step": i, "action": step.name, "kind": "timeout", "name": "SCENARIO_TIMEOUT",
                                       "expected": {"max_ms": sc.timeout_ms}, "actual": {"elapsed_ms": s.elapsed()},
                                       "message": "Senaryo süresi doldu"})
                    break
                fails = s.run_step(step, i)
                if fails and not step.continue_on_failure:
                    for rest in sc.steps[i:]:
                        s.steps.append({"index": len(s.steps) + 1, "name": rest.name, "args": rest.args,
                                        "status": "skipped"})
                    break
            else:
                s.ctx.step = None
                s.failures += s.check(sc.asserts)
            s.ctx.step = None
            s.implicit_checks(sc.oracle)
        except BridgeError as e:
            s.fail_infra(f"Bridge hatası: {e}")
        except Exception as e:  # beklenmeyen hata da raporlansın
            s.fail_infra(f"Beklenmeyen hata: {type(e).__name__}: {e}")
        return s.finish()


def load_run_report(store: Store, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(f"Run bulunamadı: {run_id}")
    p = Path(run["artifacts_dir"]) / "report.json"
    if not p.exists():
        return {**run, "result": run["status"], "note": "report.json henüz yok (run devam ediyor olabilir)"}
    return json.loads(p.read_text(encoding="utf-8"))
