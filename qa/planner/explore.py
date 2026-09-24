"""EXPLORE modu: Claude planlayıcı, bu modül yürütücü.

LLM her tuşa basmaz. Claude bir hedef verir ("offline shop'u 30 dk oyuncu gibi kullan"),
`checklist` ile edge-case fikirleri alır, sonra davranış seviyesinde adımlar gönderir
(kill_monster, buy_item ...). Her adım gerçek oyuncu yolundan yürütülür, trace'e yazılır ve
sonucu + durum farkı + yeni sunucu hataları Claude'a döner. Keşif bitince bulgular rapora
yazılır ve istenirse yürütülen adımlar regression senaryosu olarak kaydedilir.

Döngü: Plan → Execute (step) → Observe (state/diff) → Verify (check) → Reflect → sonraki adım.
"""

from __future__ import annotations

import threading
from typing import Any

from ..config import QaConfig
from ..oracle.assertions import take_snapshot
from ..scenario.loader import dump_scenario, save_scenario
from ..scenario.runner import RunSession
from ..scenario.schema import AssertSpec, OracleOptions, Scenario, SetupOp, Step
from ..session import BridgeFactory
from ..store.db import Store

GENERIC_CHECKS = [
    "Yeniden bağlan (reconnect) sonrası durum korunuyor mu? (DB kalıcılığı)",
    "Kanal değiştirme sonrası durum/pencereler tutarlı mı?",
    "Karakter ölüp yeniden doğunca sistem bozuluyor mu?",
    "Envanter doluyken işlem ne yapıyor? (item kaybı?)",
    "Yetersiz yang ile deneme — reddedilmeli, yang eksiye düşmemeli",
    "Sınır değerler: 0, 1, maksimum, maksimum+1",
    "Aynı işlemi hızlı art arda tekrarlama (çift işlem / duplikasyon)",
    "Pencere açıkken uzaklaşma / pencereyi kapatıp tekrar açma",
    "Her adımdan sonra server_errors ve qa_asserts 0 kalmalı",
]

AREA_CHECKS: dict[tuple[str, ...], list[str]] = {
    ("shop", "pazar", "dükkan", "market", "offline"): [
        "Kendi itemini satın almayı dene",
        "Fiyat = 1 ve fiyat = maksimum",
        "Satıcı çevrimdışıyken alım",
        "Alıcının envanteri doluyken alım (item/yang kaybı?)",
        "Aynı itemi iki alıcı aynı anda almaya çalışsın (çoklu ajan)",
    ],
    ("trade", "ticaret"): [
        "İki taraf onayladıktan sonra item ekleme/çıkarma",
        "Trade sırasında bağlantı kopması",
        "Yang taşması (maksimum yang + trade)",
    ],
    ("quest", "görev", "battle", "pass", "mission"): [
        "İlerleme sayacı: her olay tam 1 artıyor mu? (off-by-one)",
        "Hedef tamamlanınca ödül tam bir kez veriliyor mu?",
        "Görev ortasında reconnect/kanal değişimi ilerlemeyi koruyor mu?",
        "Ödülü envanter doluyken almaya çalış",
    ],
    ("item", "envanter", "inventory", "equip", "upgrade", "yükselt"): [
        "Stack sınırı (200) ve stack bölme",
        "Giyili itemi değiştirme/çıkarma, envanter doluyken çıkarma",
        "Yükseltme başarı/başarısızlık, malzeme eksikken yükseltme",
    ],
    ("pet", "mount", "binek"): [
        "Çağır → ışınlan → kanal değiştir → öl → reconnect → tekrar çağır",
        "Çift çağırma (duplike pet)",
        "Bonuslar çağırınca eklenip geri çağırınca siliniyor mu?",
    ],
    ("combat", "savaş", "skill", "mob", "boss", "dungeon", "zindan"): [
        "Hasar/ölüm/drop olayları sunucu olaylarıyla tutarlı mı?",
        "Ölüm sırasında iksir kullanma / skill",
        "Drop başkasına mı düşüyor, yerde kalıyor mu?",
    ],
}


def suggest_checklist(goal: str) -> dict[str, Any]:
    g = goal.lower()
    areas = {k[0]: v for k, v in AREA_CHECKS.items() if any(w in g for w in k)}
    return {"goal": goal, "area_checks": areas, "generic_checks": GENERIC_CHECKS,
            "loop": "Plan → explore_step → gözlemle (state/diff/server_errors) → explore_check → düşün → sonraki adım"}


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ("level", "exp", "hp", "sp", "gold", "x", "y", "map", "channel", "dead"):
        if before["state"].get(k) != after["state"].get(k):
            out[k] = [before["state"].get(k), after["state"].get(k)]

    def counts(inv: dict[str, Any]) -> dict[int, int]:
        c: dict[int, int] = {}
        for i in inv["items"]:
            c[i["vnum"]] = c.get(i["vnum"], 0) + i["count"]
        return c

    cb, ca = counts(before["inventory"]), counts(after["inventory"])
    inv = {v: ca.get(v, 0) - cb.get(v, 0) for v in set(cb) | set(ca) if ca.get(v, 0) != cb.get(v, 0)}
    if inv:
        out["inventory"] = inv
    if before["inventory"].get("equipment") != after["inventory"].get("equipment"):
        out["equipment"] = after["inventory"].get("equipment")
    if before["quests"] != after["quests"]:
        out["quests"] = after["quests"]
    return out


class ExplorationManager:
    def __init__(self, cfg: QaConfig, store: Store, factory: BridgeFactory | None = None):
        self.cfg, self.store = cfg, store
        self.factory = factory or BridgeFactory(cfg)
        self.sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _get(self, run_id: str) -> dict[str, Any]:
        s = self.sessions.get(run_id)
        if s is None:
            raise KeyError(f"Açık keşif oturumu yok: {run_id}")
        return s

    def start(self, goal: str, account: str | None = None, seed: int | None = None,
              setup: list[Any] | None = None, faults: list[str] | None = None) -> dict[str, Any]:
        ops = [SetupOp.parse(x) for x in setup or []]
        rs = RunSession(self.cfg, self.store, self.factory, scenario="explore", mode="explore", seed=seed,
                        account=account, faults=faults)
        try:
            rs.start()
            rs.setup(ops)
        except Exception as e:  # login/setup/bridge — hepsi keşfi başlatamaz
            rs.fail_infra(f"Keşif başlatılamadı: {e}")
            rep = rs.finish({"goal": goal})
            return {"run_id": rs.run_id, "ok": False, "error": rep["error"]}
        with self._lock:
            self.sessions[rs.run_id] = {"rs": rs, "goal": goal, "setup": ops, "steps": [], "checks": []}
        return {"run_id": rs.run_id, "ok": True, "seed": rs.seed, "account": rs.account,
                "state": rs.baseline["state"], "inventory": rs.baseline["inventory"],
                "nearby": rs.ctx.entities(radius=8000)[:30], "checklist": suggest_checklist(goal)}

    def step(self, run_id: str, raw_step: Any) -> dict[str, Any]:
        s = self._get(run_id)
        rs: RunSession = s["rs"]
        step = Step.parse(raw_step)
        before = take_snapshot(rs.ctx)
        n_events = len(rs.ctx.events)
        n_err = len(rs.signals.errors)
        n_assert = len(rs.signals.assert_failures)
        idx = len(s["steps"]) + 1
        fails = rs.run_step(step, idx)
        rs.signals.poll()
        after = take_snapshot(rs.ctx)
        s["steps"].append(step)
        rec = rs.steps[-1]
        return {
            "step": idx,
            "status": rec["status"],
            "result": rec.get("result"),
            "error": rec.get("error"),
            "failures": fails,
            "diff": _diff(before, after),
            "client_events": [{"event": e["event"], "data": e["data"]} for e in rs.ctx.events[n_events:]][-30:],
            "new_server_errors": rs.signals.errors[n_err:],
            "new_qa_assert_failures": rs.signals.assert_failures[n_assert:],
            "state": after["state"],
            "windows": rs.ctx.windows(),
            "game_time_ms": rs.elapsed(),
        }

    def check(self, run_id: str, asserts: list[Any]) -> dict[str, Any]:
        s = self._get(run_id)
        rs: RunSession = s["rs"]
        specs = [AssertSpec.parse(a) for a in asserts]
        fails = rs.check(specs, step=len(s["steps"]) or None)
        rs.failures += fails
        s["checks"] += specs
        results = rs.assertions[-len(specs):] if specs else []
        return {"passed": not fails, "results": results}

    def observe(self, run_id: str) -> dict[str, Any]:
        rs: RunSession = self._get(run_id)["rs"]
        return {"state": rs.ctx.state(), "inventory": rs.ctx.inventory(), "quests": rs.ctx.query("get_quest_state"),
                "windows": rs.ctx.windows(), "nearby": rs.ctx.entities(radius=8000)[:30],
                "messages": rs.ctx.query("get_system_messages", since=0)[-10:]}

    def finish(self, run_id: str, findings: list[dict[str, Any]] | None = None,
               save_as_scenario: str | None = None, overwrite: bool = False) -> dict[str, Any]:
        with self._lock:
            s = self.sessions.pop(run_id, None)
        if s is None:
            raise KeyError(f"Açık keşif oturumu yok: {run_id}")
        rs: RunSession = s["rs"]
        findings = findings or []
        for f in findings:
            # Claude'un bulduğu (assertion dışı, ör. görsel) hatalar da rapora hata olarak girer
            if f.get("severity", "bug") in {"bug", "critical", "major"}:
                rs.failures.append({"step": f.get("step"), "action": None, "kind": "finding",
                                    "name": f.get("title", "finding"), "expected": f.get("expected"),
                                    "actual": f.get("actual"), "message": f.get("description", "")})
        rs.ctx.step = None
        rs.implicit_checks(OracleOptions())
        saved = None
        if save_as_scenario:
            sc = Scenario(name=save_as_scenario, description=f"Keşiften üretildi ({run_id}): {s['goal']}",
                          tags=["generated", "explore"], account=rs.account, seed=rs.seed,
                          setup=s["setup"], steps=s["steps"], asserts=s["checks"])
            text = dump_scenario(sc)
            saved = str(save_scenario(self.cfg.scenarios_path, save_as_scenario, text, overwrite))
        rep = rs.finish({"goal": s["goal"], "findings": findings, "saved_scenario": saved})
        return {"run_id": run_id, "result": rep["result"], "summary": rep["summary"], "saved_scenario": saved,
                "failures": rep["failures"]}

