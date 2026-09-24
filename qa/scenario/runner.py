"""Senaryo çalıştırıcı.

Akış: bağlan → (sim ise dünyayı seed ile sıfırla) → login → karakter seç → /qa reset + setup →
başlangıç görüntüsü → adımlar (+ satır içi expect) → final assert → örtük oracle kontrolleri
(SYSERR yok, QA_ASSERT ihlali yok) → kanıt topla → rapor.

RunSession hem sabit senaryolar hem de Claude'un adım adım yönettiği keşif (explore) modu
tarafından kullanılır.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..bridge.client import Bridge
from ..bridge.protocol import ActionError, BridgeError
from ..config import QaConfig
from ..engine.behaviours import login
from ..engine.executor import BehaviourError, GameContext, Trace, execute_step
from ..engine.rng import QaRandom, new_seed
from ..oracle.assertions import GROUP_ASSERTIONS, AssertionResult, Observation, evaluate, take_snapshot
from ..oracle.signals import BridgeEventSource, EventSource, FileEventSource, LogFileSource, ServerSignals
from ..session import BridgeFactory
from ..sim.world import SimWorld
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


@dataclass
class Agent:
    """Run'daki bir oyuncu: kendi bridge'i (istemcisi), bağlamı ve başlangıç görüntüsü."""
    name: str                     # tek ajanlı senaryoda ""
    account: str
    character: str
    index: int = 0
    bridge: Bridge | None = None
    ctx: GameContext | None = None
    baseline: dict[str, Any] | None = None


class RunSession:
    def __init__(self, cfg: QaConfig, store: Store, factory: BridgeFactory, *, scenario: str, mode: str,
                 seed: int | None, account: str | None = None, character: str | None = None,
                 scenario_text: str | None = None, faults: list[str] | None = None,
                 replay_of: str | None = None, agents: dict[str, Any] | None = None):
        cfg.check_environment()
        self.cfg, self.store = cfg, store
        # Sim modunda tüm ajanlar aynı dünyayı paylaşmalı: run'a özel dünya
        if factory.is_sim and factory.world is None:
            factory = BridgeFactory(cfg, SimWorld(password=cfg.accounts.password))
        self.factory = factory
        self.scenario, self.mode = scenario, mode
        self.seed = seed if seed is not None else (cfg.default_seed if cfg.default_seed is not None else new_seed())
        if not agents:
            agents = {"": {"account": account, "character": character}}
        self.agents: dict[str, Agent] = {}
        for i, (name, spec) in enumerate(agents.items()):
            spec = spec if isinstance(spec, dict) else spec.model_dump()
            acc = spec.get("account") or cfg.accounts.default_account
            char = spec.get("character") or acc
            cfg.check_account(acc)
            cfg.check_account(char)
            self.agents[name] = Agent(name, acc, char, i)
        self.primary = next(iter(self.agents.values()))
        self.account, self.character = self.primary.account, self.primary.character
        self.multi = len(self.agents) > 1 or self.primary.name != ""
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
        self.trace = Trace()
        self.signals: ServerSignals | None = None
        self.steps: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self.error: str | None = None
        self._t0 = 0
        self.finished = False
        self.report: dict[str, Any] | None = None

    # Tek ajanlı kullanım (keşif modu vb.) için birincil ajan kısayolları
    @property
    def ctx(self) -> GameContext | None:
        return self.primary.ctx

    @property
    def bridge(self) -> Bridge | None:
        return self.primary.bridge

    @property
    def baseline(self) -> dict[str, Any] | None:
        return self.primary.baseline

    def agent(self, name: str | None) -> Agent:
        if name is None:
            return self.primary
        if name not in self.agents:
            raise BehaviourError("UNKNOWN_AGENT", f"Ajan bulunamadı: {name}")
        return self.agents[name]

    def _contexts(self) -> list[GameContext]:
        return [a.ctx for a in self.agents.values() if a.ctx is not None]

    def _set_step(self, step: int | None) -> None:
        for c in self._contexts():
            c.step = step

    # ------------------------------------------------------------------ başlangıç
    def start(self) -> None:
        """Tüm ajanlar için bağlan ve giriş yap."""
        for a in self.agents.values():
            a.bridge = self.factory.open(a.account, a.index)
            a.bridge.connect()
        caps = self.primary.bridge.capabilities
        if "sim_control" in caps:
            self.primary.bridge.call("sim_reset", seed=self.seed, faults=self.faults)
        root_rng = QaRandom(self.seed)
        for a in self.agents.values():
            rng = root_rng if a is self.primary else root_rng.fork(a.name)
            sink = self.artifacts.save_screenshot
            if self.multi:
                sink = (lambda n: lambda label, raw, ext: self.artifacts.save_screenshot(f"{n}_{label}", raw, ext))(a.name)
            a.ctx = GameContext(a.bridge, rng, trace=self.trace, account=a.account,
                                password=self.cfg.accounts.password, character=a.character,
                                screenshot_sink=sink, agent=a.name or None)
        peers = {a.name: a.ctx for a in self.agents.values() if a.name}
        for a in self.agents.values():
            a.ctx.peers = peers
        self.primary.ctx.note("run_start", run_id=self.run_id, scenario=self.scenario, seed=self.seed,
                              agents={a.name: a.account for a in self.agents.values()} if self.multi else None)
        self.signals = ServerSignals(self._event_sources(caps),
                                     characters={a.character for a in self.agents.values()})
        self.signals.mark()
        self._t0 = self.primary.ctx.now()
        self._set_step(0)
        for a in self.agents.values():
            login(a.ctx)

    def _event_sources(self, caps: set[str]) -> list[EventSource]:
        sc = self.cfg.server
        sources: list[EventSource] = []
        if sc.events_file:
            sources.append(FileEventSource(self.cfg.resolve(sc.events_file)))
        elif "server_events" in caps:
            sources.append(BridgeEventSource(self.primary.bridge))
        for name, path in sc.log_files.items():
            sources.append(LogFileSource(name, self.cfg.resolve(path), sc.error_patterns))
        return sources

    def qa_command(self, command: str, agent: str | None = None) -> str:
        return send_qa_command(self.agent(agent).ctx, command)

    def setup(self, ops: list[SetupOp], reset: bool = True) -> None:
        self._set_step(0)
        for a in self.agents.values():
            if reset:
                send_qa_command(a.ctx, "/qa reset")
            for op in ops:
                if self.agent(op.agent) is a:
                    send_qa_command(a.ctx, op.to_command())
        self.primary.ctx.wait(300)
        for a in self.agents.values():
            a.baseline = take_snapshot(a.ctx)
            a.ctx.note("baseline", state=a.baseline["state"])
        self.signals.poll()

    # ------------------------------------------------------------------ adımlar
    def run_step(self, step: Step, index: int) -> list[dict[str, Any]]:
        a = self.agent(step.agent)
        ctx = a.ctx
        self._set_step(index)
        who = {"agent": a.name} if a.name else {}
        rec: dict[str, Any] = {"index": index, "name": step.name, "args": step.args, "label": step.label,
                               **who, "t_start": ctx.now()}
        ctx.note("step_start", name=step.name, args=step.args, label=step.label)
        fails: list[dict[str, Any]] = []
        try:
            result = execute_step(ctx, step.name, step.args)
            rec["result"] = _jsonable(result)
            if step.expect_error:
                fails.append({"step": index, "action": step.name, **who, "label": step.label, "kind": "assertion",
                              "name": "expect_error", "expected": {"error": step.expect_error},
                              "actual": {"error": None, "result": rec["result"]},
                              "message": f"Adımın {step.expect_error} ile reddedilmesi bekleniyordu ama başarılı oldu"})
        except BehaviourError as e:
            got = {e.code, e.details.get("reject_code")}
            if step.expect_error and step.expect_error in got:
                rec["result"] = {"rejected": step.expect_error}
            else:
                rec["error"] = f"{e.code}: {e.message}"
                fails.append({"step": index, "action": step.name, **who, "label": step.label,
                              "kind": "action_error", "name": e.code,
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
        group: dict[str, Observation] = {}
        for a in self.agents.values():
            group[a.name] = Observation(a.ctx, a.baseline or take_snapshot(a.ctx), self.signals)
        for o in group.values():
            o.group = group
        fails = []
        for spec in specs:
            a = self.agent(spec.agent)
            r = evaluate(group[a.name], spec.name, spec.args)
            label = "" if spec.name in GROUP_ASSERTIONS and spec.agent is None else a.name
            self._record_assertion(r, step, label)
            if not r.passed:
                f = self._assert_failure(r, step, action)
                if label:
                    f["agent"] = label
                fails.append(f)
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

    def _record_assertion(self, r: AssertionResult, step: int | None, agent: str = "") -> None:
        d = r.to_dict()
        d["step"] = step
        if agent:
            d["agent"] = agent
        d["actual"] = _jsonable(d["actual"])
        self.assertions.append(d)
        (self.agents[agent].ctx if agent in self.agents else self.primary.ctx).note(
            "assert", name=r.name, passed=r.passed)

    @staticmethod
    def _assert_failure(r: AssertionResult, step: int | None, action: str | None) -> dict[str, Any]:
        return {"step": step, "action": action, "kind": "assertion", "name": r.name, "params": r.params,
                "expected": r.expected, "actual": _jsonable(r.actual), "message": r.message or "ASSERTION FAILED"}

    def _failure_screenshot(self, index: int) -> None:
        for c in self._contexts():
            try:
                c.screenshot(f"failure_step_{index}")
            except (ActionError, BridgeError, OSError):
                pass

    def elapsed(self) -> int:
        return self.primary.ctx.now() - self._t0 if self.primary.ctx else 0

    # ------------------------------------------------------------------ bitiş
    def fail_infra(self, message: str) -> None:
        self.error = message

    def _shutdown_agent(self, a: Agent) -> str:
        ctx, log = a.ctx, ""
        if ctx is not None and a.bridge is not None:
            try:
                if ctx.state().get("in_game"):
                    ctx.step = None
                    ctx.act("logout")
            except (ActionError, BridgeError):
                pass
            try:
                log = "\n".join(f"[{m['t']}] {m['text']}" for m in ctx.query("get_client_log", since=0))
            except (ActionError, BridgeError):
                log = "(istemci log komutunu desteklemiyor)"
        return log

    def finish(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.finished:
            return self.report
        self.finished = True
        logs = {a.name: self._shutdown_agent(a) for a in self.agents.values()}
        try:
            if self.signals:
                self.signals.poll()
        except (ActionError, BridgeError, OSError):
            pass
        for a in self.agents.values():
            if a.bridge is not None:
                a.bridge.close()
        if self.multi:
            client_log = "\n\n".join(f"== {n} ({self.agents[n].account}) ==\n{l}" for n, l in logs.items())
        else:
            client_log = logs[self.primary.name]

        if self.error:
            result = "ERROR"
        elif self.failures:
            result = "FAILED"
        else:
            result = "PASSED"

        a = self.artifacts
        evidence: dict[str, Any] = {"report": "report.json"}
        started = self.primary.ctx is not None
        if started:
            self.trace.write_jsonl(a.path("trace.jsonl"))
            evidence["action_trace"] = "trace.jsonl"
            evidence["action_trace_text"] = a.write_text("trace.txt", self.trace.format_text())
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
            "agents": {x.name: x.account for x in self.agents.values()} if self.multi else None,
            "replay_of": self.replay_of,
            "build": {**self.build, "env": self.cfg.env},
            "client": self.primary.bridge.info if self.primary.bridge else None,
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
            "trace_digest": self.trace.digest() if started else None,
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
                       scenario_text=scenario_text, faults=sc.sim_faults, replay_of=replay_of,
                       agents=sc.agents or None)
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
                s._set_step(None)
                s.failures += s.check(sc.asserts)
            s._set_step(None)
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
