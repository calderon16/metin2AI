"""Kampanya: "oyundaki her şeyi test et".

Katalogdaki (catalog/systems.yaml) her sistem için o sistemin senaryolarını çalıştırır; LLM tanımlıysa
sistemin keşif hedefiyle otonom keşif yapar. Sonuç sistem × durum matrisidir:
    passed   — senaryoların hepsi geçti (ve keşifte ciddi bulgu yok)
    failed   — en az bir senaryo başarısız
    findings — senaryolar geçti ama keşif ciddi bulgu raporladı
    error    — altyapı hatası (bağlantı, setup ...)
    untested — senaryosu yok ve keşif yapılmadı  ← neyin test edilmediği görünür
Önceki kampanyayla karşılaştırılarak yeni kırmızılar (regresyon) ve düzelenler çıkarılır.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..planner.budget import BudgetExceeded
from ..store.db import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from .jobs import JobContext

SERIOUS = {"critical", "major", "bug"}


def load_catalog(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    out = []
    for s in data:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        out.append({"id": s["id"], "name": s.get("name", s["id"]), "tags": list(s.get("tags") or []),
                    "explore": (s.get("explore") or "").strip() or None, "agents": int(s.get("agents", 1)),
                    "covers": list(s.get("covers") or [])})
    return out


def scenarios_for(system: dict[str, Any], scenarios: list[dict[str, Any]]) -> list[str]:
    tags = set(system["tags"])
    return [s["name"] for s in scenarios if "error" not in s and tags & set(s.get("tags", []))]


def run_campaign(ctx: "JobContext", systems: list[str] | None = None, explore: bool | None = None,
                 seed: int | None = None, explore_steps: int | None = None) -> dict[str, Any]:
    d = ctx.daemon
    catalog = d.catalog()
    if systems:
        unknown = set(systems) - {s["id"] for s in catalog}
        if unknown:
            raise ValueError(f"Katalogda olmayan sistem: {sorted(unknown)}")
        catalog = [s for s in catalog if s["id"] in systems]
    scenarios = d.service.list_scenarios()
    do_explore = d.llm_available() if explore is None else (explore and d.llm_available())
    cid = d.db.create_campaign(ctx.job_id)
    ctx.progress(campaign_id=cid, total=len(catalog), done=0)
    matrix: dict[str, Any] = {}
    cache: dict[str, dict[str, Any]] = {}   # bir senaryo birden çok sistemde olsa da bir kez koşar
    t0 = time.monotonic()
    try:
        for i, sys in enumerate(catalog):
            ctx.check_cancel()
            ctx.progress(current=sys["id"], done=i)
            st0 = time.monotonic()
            names = scenarios_for(sys, scenarios)
            runs = []
            for n in names:
                ctx.check_cancel()
                if n not in cache:
                    rep = ctx.run_scenario(n, seed=seed, system=sys["id"])
                    cache[n] = {"scenario": n, "run_id": rep["run_id"], "result": rep["result"],
                                "summary": rep["summary"]}
                runs.append(cache[n])
            exp = None
            if do_explore and sys["explore"] and sys["agents"] <= 1:
                ctx.check_cancel()
                blocked = d.llm_budget().blocked_reason()
                try:
                    if blocked:
                        raise BudgetExceeded(blocked)
                    out = ctx.explore(sys["explore"], max_steps=explore_steps, system=sys["id"])
                    serious = [f for f in out.get("findings", []) if f.get("severity") in SERIOUS]
                    exp = {"run_id": out["run_id"], "result": out["result"], "findings": len(out.get("findings", [])),
                           "serious": len(serious), "stop_reason": out.get("stop_reason"),
                           "summary": out.get("agent_summary")}
                except BudgetExceeded as e:  # bütçe doldu: keşif atlanır, hata sayılmaz
                    exp = {"skipped": str(e)}
                except Exception as e:  # keşif hatası kampanyayı durdurmasın
                    exp = {"error": f"{type(e).__name__}: {e}"}
            results = [r["result"] for r in runs]
            if not runs and (not exp or exp.get("skipped")):
                status = "untested"
            elif "FAILED" in results:
                status = "failed"
            elif "ERROR" in results or (exp and exp.get("error")):
                status = "error"
            elif exp and exp.get("serious"):
                status = "findings"
            else:
                status = "passed"
            matrix[sys["id"]] = {"name": sys["name"], "status": status, "scenarios": runs, "explore": exp,
                                 "duration_s": round(time.monotonic() - st0, 1)}
            d.db.update_campaign(cid, matrix=matrix)
        prev = d.db.last_finished_campaign(before_id=cid)
        pm = (prev or {}).get("matrix") or {}
        regressions = [k for k, v in matrix.items() if v["status"] == "failed" and pm.get(k, {}).get("status") == "passed"]
        fixed = [k for k, v in matrix.items() if v["status"] == "passed" and pm.get(k, {}).get("status") == "failed"]
        counts: dict[str, int] = {}
        for v in matrix.values():
            counts[v["status"]] = counts.get(v["status"], 0) + 1
        summary = {"counts": counts, "systems": len(matrix), "regressions": regressions, "fixed": fixed,
                   "previous_campaign": (prev or {}).get("id"), "explore": do_explore,
                   "duration_s": round(time.monotonic() - t0, 1)}
        d.db.update_campaign(cid, status="done", finished_at=utcnow(), matrix=matrix, summary=summary)
        ctx.progress(done=len(catalog), current=None)
        return {"campaign_id": cid, **summary}
    except BaseException as e:
        d.db.update_campaign(cid, status="cancelled" if isinstance(e, InterruptedError) else "failed",
                             finished_at=utcnow(), matrix=matrix, summary={"error": str(e)})
        raise
