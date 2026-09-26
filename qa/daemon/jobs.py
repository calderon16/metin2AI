"""İş kuyruğu: senaryo, suite, affected, keşif, kampanya, replay ve bulgu doğrulama işleri.

İşler SQLite'ta tutulur (daemon yeniden başlasa da geçmiş kalır). Worker thread'leri sıradaki işi
alır, gereken sayıda ajanı kiralar ve mevcut ScenarioRunner / AutoExplorer / replay_run ile çalıştırır.
Gerçek sunucuda işler paralel; sim modunda ortak dünya tek kilitle sırayla kullanılır.
İptal, run'lar arasında (ve ajan beklerken) uygulanır.
"""

from __future__ import annotations

import contextlib
import json
import threading
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..planner.autonomous import AutoExplorer, ExploreBudget
from ..planner.budget import BudgetedProvider
from ..engine import npcdir
from ..planner.llm import Message
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
    "explore_rotation": "Eğitim verisi için sıradaki keşif hedefi (training/collect_goals.yaml). params: goals?",
    "play_session": "Oyuncu modu oturumu: normal oyuncu gibi oyna (görev, kasılma, ekipman, + basma). "
                    "params: account",
    "owner_command": "Sahibin (daemon.owners) fısıltıyla verdiği iş. params: account, sender, text",
    "learning_cycle": "Veri → eğitim (Kaggle/incoming) → Ollama → değerlendirme → daha iyiyse devreye alma. "
                      "params: base_ollama?, base_unsloth?, min_samples?, gguf?",
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

    def _lease(self, count: int, only: list[str] | None = None):
        return self.daemon.agents.lease(count, self.job_id, cancelled=self.cancelled,
                                        timeout_s=self.daemon.lease_timeout_s, only=only)

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
                setup: list[Any] | None = None, seed: int | None = None, system: str | None = None,
                provider: Any = None, account: str | None = None, player: bool = False,
                should_stop: Any = None, exclude_tools: set[str] | None = None) -> dict[str, Any]:
        """player=True: oyuncu modu (reset/qa_setup/ışınlanma yok). should_stop(ajan) her turdan önce sorulur."""
        d = self.daemon
        provider = provider or d.make_llm_provider()
        e = d.cfg.explorer
        budget = ExploreBudget(max_steps=max_steps or e.max_steps, max_total_tokens=e.max_total_tokens,
                               history_turns=e.history_turns)
        with self._lease(1, [account] if account else None) as (agents, factory), d.agents.world_guard():
            stop = (lambda: bool(self.cancelled() or should_stop(agents[0]))) if should_stop else self.cancelled
            out = AutoExplorer(d.cfg, d.store, provider, factory).run(
                goal, budget=budget, account=agents[0].account, seed=seed, setup=setup,
                save_as_scenario=save_as, validate=False, player=player, should_stop=stop,
                exclude_tools=exclude_tools).to_dict()
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
            names = [s["name"] for s in d.service.runnable_scenarios(p.get("tag")) if "error" not in s]
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
    if job_type == "explore_rotation":
        return _explore_rotation(ctx, p)
    if job_type == "learning_cycle":
        return _learning_cycle(ctx, p)
    if job_type == "owner_command":
        return _owner_command(ctx, p)
    if job_type == "play_session":
        return _play_session(ctx, p)
    raise ValueError(f"Bilinmeyen iş tipi: {job_type}")


def _explore_rotation(ctx: "JobContext", p: dict[str, Any]) -> dict[str, Any]:
    """Veri toplama: hedef listesinde sırayla bir sonraki keşif (sıra artifacts/daemon/rotation.json'da)."""
    import yaml

    d = ctx.daemon
    path = d.cfg.resolve(Path(p.get("goals", "training/collect_goals.yaml")))
    goals = yaml.safe_load(path.read_text(encoding="utf-8"))["goals"]
    state_file = d.cfg.resolve(d.cfg.artifacts_dir) / "daemon" / "rotation.json"
    try:
        idx = json.loads(state_file.read_text(encoding="utf-8")).get("next", 0)
    except (OSError, ValueError):
        idx = 0
    g = goals[idx % len(goals)]
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"next": (idx + 1) % len(goals), "last": g["id"]}), encoding="utf-8")
    ctx.progress(goal=g["id"])
    out = ctx.explore(g["goal"], g.get("steps"), None, g.get("setup") or None)
    return {"goal": g["id"], **{k: out.get(k) for k in ("run_id", "result", "summary", "stop_reason")}}


OWNER_TASK_GOAL = """Sahibin {sender} fısıltıyla şunu istedi: «{text}»
Yapılacak iş: {task}
- Uzaktaki bir NPC'ye (Silah Satıcısı, Demirci ...) go_to_npc ile adıyla git; açılan diyalogda
  windows.dialog.options listesinden index ile seç.
- Bir oyuncunun ({sender}) yanına go_to_player ile git. Ticaret: trade_with(name="{sender}").
- Sahibine sen yazma; sonuç raporu yaptığın adımlardan otomatik gider. Bitince ya da yapamazsan finish çağır ve
  özetinde neyi yapıp neyi yapamadığını dürüstçe yaz."""

CHAT_REPLY_SYSTEM = """Sen Metin2'de {account} adlı karakteri oynuyorsun; {sender} (GM) sana fısıltıyla yazdı.
Ona kısa (tek cümle), samimi ve doğal bir Türkçe cevap ver. Bu yalnız sohbet: bir iş yaptığını ya da
yapacağını SÖYLEME, sayı ya da bilgi uydurma. Durumun: {state}. Yalnız cevap cümlesini yaz."""

_MEMORY_LOCK = threading.Lock()
CHAT_HISTORY = 8


def _update_memory(ctx: "JobContext", acc: str, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    path = _player_memory_path(ctx)
    with _MEMORY_LOCK:
        try:
            memory = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            memory = {}
        entry = memory.setdefault(acc, {})
        fn(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(memory, ensure_ascii=False, indent=1), encoding="utf-8")
        return entry


def _read_memory(ctx: "JobContext", acc: str) -> dict[str, Any]:
    try:
        return json.loads(_player_memory_path(ctx).read_text(encoding="utf-8")).get(acc) or {}
    except (OSError, ValueError):
        return {}


def _chat_fn(d: Any) -> Callable[[str, str], str]:
    provider = BudgetedProvider(d.make_llm_provider(), d.llm_budget())

    def chat(system: str, user: str) -> str:
        try:
            return provider.chat(system, [Message("user", text=user)], []).message.text or ""
        except Exception:  # noqa: BLE001 — model yoksa kurallar ve kod yine çalışır
            return ""
    return chat


def _one_line(text: str, limit: int = 180) -> str:
    t = (text or "").strip()
    if "</think>" in t:
        t = t.rsplit("</think>", 1)[1]
    t = t.strip().strip('"').replace("\n", " ")
    return t[:limit]


def _owner_command(ctx: "JobContext", p: dict[str, Any]) -> dict[str, Any]:
    try:
        return _owner_command_inner(ctx, p)
    except InterruptedError:
        raise
    except Exception:
        # Beklenmedik hata: sahip cevapsız kalmasın, ama olmayan bir başarı da söylenmesin
        with contextlib.suppress(Exception), ctx._lease(1, [p["account"]], ) as (agents, _f):
            agents[0].bridge.call("whisper", to=p["sender"], message="Bir hata oldu, bunu yapamadım.")
        raise


def _owner_command_inner(ctx: "JobContext", p: dict[str, Any]) -> dict[str, Any]:
    """Sahibin fısıltısı (bkz. qa/daemon/owner.py): niyet kurallarla (gerekirse modelle, sabit listeden) anlaşılır,
    bilinen işler davranış adımlarıyla yürütülür ve sahibe giden rapor adımların GERÇEK sonucundan üretilir."""
    from ..planner.explore import ExplorationManager
    from ..scenario.runner import load_run_report
    from . import owner as O

    d, acc, sender = ctx.daemon, p["account"], p["sender"]
    text = str(p["text"])[:400]
    ctx.progress(account=acc, text=text[:80])
    chat = _chat_fn(d)
    intent = O.classify(text, chat, sender, acc)
    ctx.progress(intent=intent.kind, intent_source=intent.source)
    mem = _read_memory(ctx, acc)
    activity = "bekliyorum" if mem.get("paused") else (
        "oyunu oynuyordum (görev, avlanma, ekipman)" if d.cfg.daemon.player_mode else "boştaydım")
    out: dict[str, Any] = {"account": acc, "intent": intent.kind, "intent_source": intent.source}
    said: list[str] = []

    with ctx._lease(1, [acc]) as (agents, factory), d.agents.world_guard():
        em = ExplorationManager(d.cfg, d.store, factory)
        start = em.start(f"Sahip komutu ({sender}): {text}", acc, None, None, reset=False)
        if not start["ok"]:
            raise RuntimeError(f"Komut başlatılamadı: {start['error']}")
        run_id = start["run_id"]
        out["run_id"] = run_id
        ctx._record(run_id, agents)

        def step(_behaviour: str, **args: Any) -> dict[str, Any]:
            ctx.check_cancel()
            return em.step(run_id, {_behaviour: args or None})

        def say(msg: str) -> None:
            said.append(msg)
            step("whisper", to=sender, message=msg[:200])

        def ok(r: dict[str, Any]) -> bool:
            return r.get("status") == "passed"

        def err(r: dict[str, Any]) -> str:
            return O.clean_error(r.get("error"))

        final = None
        k = intent.kind
        if k == "chat":
            st = start["state"]
            facts = {x: st.get(x) for x in ("level", "gold", "hp", "max_hp")}
            reply = _one_line(chat(CHAT_REPLY_SYSTEM.format(account=acc, sender=sender,
                                                           state=json.dumps(facts, ensure_ascii=False)), text))
            final = reply or "Selam!"
        elif k == "status":
            obs = em.observe(run_id)
            final = O.status_text(obs["state"], obs.get("nearby") or [], activity, text,
                                  (obs.get("inventory") or {}).get("items"), obs.get("quests"))
        elif k == "pause":
            _update_memory(ctx, acc, lambda e: e.update(paused=True))
            final = "Tamam, burada bekliyorum. Devam etmemi istersen 'devam et' yaz."
        elif k == "resume":
            _update_memory(ctx, acc, lambda e: e.update(paused=False))
            final = "Tamam, oynamaya devam ediyorum."
        elif k == "come":
            say("Geliyorum.")
            r = step("go_to_player", name=sender)
            final = "Yanındayım." if ok(r) else (
                "Seni göremiyorum; hangi haritada ve neredesin?" if "PLAYER_NOT_VISIBLE" in str(r.get("error"))
                else f"Gelemedim: {err(r)}")
        elif k == "receive_trade":
            gold0 = start["state"].get("gold") or 0
            say("Geliyorum, sana ticaret açacağım.")
            r = step("go_to_player", name=sender)
            if ok(r):
                r = step("trade_with", name=sender)
            if not ok(r):
                final = ("Seni göremiyorum; neredesin?" if "PLAYER_NOT_VISIBLE" in str(r.get("error"))
                         else f"Ticaret açamadım: {err(r)}")
            else:
                say("Ticaret penceresini açtım; teklifini koyup onayla.")
                r = step("trade_wait_and_accept", timeout_ms=120000)
                gold1 = (em.observe(run_id)["state"].get("gold") or 0)
                if ok(r) and (r.get("result") or {}).get("completed"):
                    final = f"Aldım, teşekkürler! (+{gold1 - gold0} yang)" if gold1 > gold0 else "Aldım, teşekkürler!"
                else:
                    final = f"Ticaret tamamlanmadı: {err(r) or 'pencere kapandı'}"
        elif k == "give":
            obs = em.observe(run_id)
            st, inv = obs["state"], (obs.get("inventory") or {}).get("items", [])
            slot = None
            final = None
            if intent.gold:
                if (st.get("gold") or 0) < intent.gold:
                    final = f"Yeterli yangım yok (şu an {st.get('gold')} yang var)."
            else:
                q = npcdir.fold(intent.target or "")
                have = [i for i in inv if q and q in npcdir.fold(i.get("name") or "")]
                if not have:
                    names = sorted({str(i.get("name") or i["vnum"]) for i in inv})
                    final = (f"İstediğin eşya envanterimde yok. Bende olanlar: {', '.join(names[:12])}." if names
                             else "İstediğin eşya envanterimde yok; envanterim boş.")
                else:
                    it = max(have, key=lambda i: i["count"])
                    slot = it["slot"]
                    if intent.count and intent.count < it["count"]:
                        r = step("split_item", slot=slot, count=intent.count)
                        if not ok(r):
                            final = f"Yığını bölemedim: {err(r)}"
                        else:
                            slot = (r.get("result") or {}).get("slot")
                    elif intent.count and intent.count > it["count"]:
                        say(f"Bende yalnız {it['count']} tane var, hepsini veriyorum.")
            if final is None:
                say("Geliyorum, ticarete koyacağım.")
                r = step("go_to_player", name=sender)
                if ok(r):
                    r = step("trade_with", name=sender)
                if ok(r):
                    r = step("trade_set_gold", amount=intent.gold) if intent.gold else step("trade_add_item", slot=slot)
                if not ok(r):
                    final = f"Veremedim: {err(r)}"
                else:
                    r = step("trade_accept")
                    if ok(r) and (r.get("result") or {}).get("completed"):
                        final = "Verdim."            # sahip zaten onaylamıştı
                    else:
                        say("Ticarete koydum ve onayladım; sen de onayla.")
                        r = step("trade_wait_and_accept", timeout_ms=120000)
                        final = ("Verdim." if ok(r) and (r.get("result") or {}).get("completed")
                                 else f"Ticaret tamamlanmadı: {err(r) or 'pencere kapandı'}")
        elif k == "hunt":
            n = max(1, min(int(intent.count or 1), 50))
            say(f"Tamam, {n} tane {intent.target} kesmeye gidiyorum.")
            r = step("kill_monster", name=intent.target, count=n, timeout_ms=min(900000, 120000 + n * 60000))
            kills = (r.get("result") or {}).get("kills", 0) if ok(r) else 0
            final = f"{kills} tane {intent.target} kestim." if ok(r) else f"Kesemedim: {err(r)}"

        if final is not None:
            say(final)
            em.finish(run_id, [], None, extra={"agent_summary": final, "stop_reason": "owner_command"})
            out.update(reply=said[0] if said else final, report=final, said=said)
        else:
            say(f"Tamam, şunu yapıyorum: {(intent.task or text)[:150]}")
            em.finish(run_id, [], None, extra={"agent_summary": "iş başlatıldı", "stop_reason": "owner_command"})

    def remember(entry: dict[str, Any]) -> None:
        chat_log = entry.setdefault("chat", [])
        chat_log += [{"from": sender, "text": text}] + [{"from": "me", "text": x} for x in said]
        del chat_log[:-CHAT_HISTORY]

    if intent.kind != "task":
        _update_memory(ctx, acc, remember)
        return out

    # Serbest iş: model oyuncu modunda yürütür (fısıltı aracı yok); rapor adımların gerçek sonucundan
    task = (intent.task or text)[:300]
    goal = OWNER_TASK_GOAL.format(sender=sender, text=text, task=task)
    res = ctx.explore(goal, d.cfg.daemon.owner_command_steps, None, None, account=acc, player=True,
                      exclude_tools={"whisper"})
    rep = load_run_report(d.store, res["run_id"]) or {}
    final = O.task_report(rep.get("steps") or [], res.get("stop_reason"))
    with ctx._lease(1, [acc]) as (agents, _f), d.agents.world_guard():
        with contextlib.suppress(Exception):
            agents[0].bridge.call("whisper", to=sender, message=final[:200])
    said.append(final)
    _update_memory(ctx, acc, remember)
    out.update(task=task, task_run_id=res.get("run_id"), report=final, said=said, stop_reason=res.get("stop_reason"))
    return out


PLAY_GOAL = """Oyunu normal bir oyuncu gibi oynamaya devam et: görevleri al ve bitir, seviye atla, daha iyi
ekipman edin ve giy, + bas. Karşılaştığın hataları kaydet.
{memory}"""


def _player_memory_path(ctx: "JobContext") -> Path:
    return Path(ctx.daemon.cfg.resolve("artifacts")) / "daemon" / "player_memory.json"


def _play_session(ctx: "JobContext", p: dict[str, Any]) -> dict[str, Any]:
    """Oyuncu modu: tek ajanla (only=[hesap]) oyuncu istemli LLM oturumu. Bir önceki oturumun özeti (plan)
    hedefe eklenir; sahip fısıldarsa oturum hemen kesilir ki komut beklemesin."""
    d, acc = ctx.daemon, p["account"]
    last = _read_memory(ctx, acc).get("summary")
    goal = PLAY_GOAL.format(memory=f"Önceki oturumun özeti ve planın:\n{last}" if last else "")
    ctx.progress(account=acc)

    def owner_waiting(agent: Any) -> bool:
        return d.agents.poll_whispers(agent, agent.bridge) > 0 or d.owner_command_pending(acc)

    out = ctx.explore(goal, d.cfg.daemon.player_session_steps, None, None, account=acc, player=True,
                      should_stop=owner_waiting, exclude_tools={"whisper"})
    summary = (out.get("agent_summary") or "").strip()
    if summary:
        _update_memory(ctx, acc, lambda e: e.update(summary=summary[:1500], run_id=out.get("run_id")))
    return {"account": acc, **{k: out.get(k) for k in ("run_id", "result", "summary", "stop_reason")}}


def _learning_cycle(ctx: "JobContext", p: dict[str, Any]) -> dict[str, Any]:
    """training/cycle.py: değerlendirme keşifleri bu işin kiraladığı ajanla koşar (oyundaki ajanlar atılmaz)."""
    import sys

    d = ctx.daemon
    root = str(d.cfg.resolve(Path(".")))
    if root not in sys.path:
        sys.path.insert(0, root)
    from training.cycle import run_cycle

    def explore_fn(goal: dict[str, Any], provider: Any) -> dict[str, Any]:
        ctx.check_cancel()
        return ctx.explore(goal["goal"], goal.get("steps"), None, goal.get("setup") or None, provider=provider)

    def artifacts_of(run_id: str) -> Path | None:
        run = d.store.get_run(run_id)
        return Path(run["artifacts_dir"]) if run else None

    return run_cycle(p, explore_fn, artifacts_of, log=lambda m: ctx.progress(last=m[:200]), should_stop=ctx.cancelled)


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
        need = {"scenario": ["name"], "explore": ["goal"], "replay": ["run_id"], "confirm": ["finding_id"],
                "play_session": ["account"], "owner_command": ["account", "text"]}
        missing = [k for k in need.get(job_type, []) if not params.get(k)]
        if missing:
            raise ValueError(f"{job_type} için eksik parametre: {missing}")
        if job_type == "scenario":
            load_scenario(self.daemon.cfg.scenarios_path, params["name"])  # yoksa hata
        llm_job = job_type in ("explore", "play_session")
        if llm_job and not self.daemon.llm_available():
            e = self.daemon.cfg.explorer
            if e.provider == "ollama":
                raise ValueError(f"Ollama'ya ulaşılamıyor ({e.ollama_url}); Ollama'yı başlatın")
            raise ValueError(f"LLM anahtarı yok: {e.api_key_env} ortam değişkenini ayarlayın")
        if llm_job:
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
