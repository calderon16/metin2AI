"""ReplayFailure: bir run'ı aynı senaryo anlık görüntüsü + aynı seed ile N kez tekrar oynatır.

Hata imzası (ilk hatanın adımı + türü + adı) her tekrarda karşılaştırılır:
"REPRODUCED 5/5" → hata gerçek ve deterministik; "REPRODUCED 2/5" → kararsız (flaky/zamanlama);
"NOT REPRODUCED 0/5" → muhtemel yanlış pozitif ya da düzeltilmiş.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .scenario.loader import parse_scenario
from .scenario.runner import ScenarioRunner, load_run_report
from .store.report import failure_signature


def replay_run(runner: ScenarioRunner, run_id: str, times: int = 3,
               accounts: list[str] | None = None) -> dict[str, Any]:
    original = load_run_report(runner.store, run_id)
    if original.get("mode") == "explore":
        raise ValueError("Keşif (explore) run'ları doğrudan tekrar oynatılamaz; önce senaryo olarak kaydedin")
    run = runner.store.get_run(run_id)
    scen_file = Path(run["artifacts_dir"]) / "scenario.yaml"
    if not scen_file.exists():
        raise FileNotFoundError(f"{run_id} için scenario.yaml anlık görüntüsü yok")
    text = scen_file.read_text(encoding="utf-8")
    sc = parse_scenario(text)
    times = max(1, min(int(times), 20))
    sig = failure_signature(original)

    runs = []
    for _ in range(times):
        rep = runner.run(sc, text, seed=original["seed"], replay_of=run_id, mode="replay", accounts=accounts)
        s = failure_signature(rep)
        runs.append({
            "run_id": rep["run_id"],
            "result": rep["result"],
            "signature": s,
            "matches": s == sig and rep["result"] == original["result"],
            "trace_digest": rep["trace_digest"],
            "summary": rep["summary"],
        })

    hits = sum(r["matches"] for r in runs)
    if original["result"] == "PASSED":
        verdict = f"STABLE PASS {hits}/{times}"
    elif hits == times:
        verdict = f"REPRODUCED {hits}/{times}"
    elif hits:
        verdict = f"FLAKY — REPRODUCED {hits}/{times}"
    else:
        verdict = f"NOT REPRODUCED 0/{times}"
    digests = {r["trace_digest"] for r in runs}
    return {
        "run_id": run_id,
        "scenario": sc.name,
        "seed": original["seed"],
        "original_result": original["result"],
        "original_signature": sig,
        "verdict": verdict,
        "reproduced": hits,
        "total": times,
        # Aynı seed → aynı trace ise davranış tamamen deterministik
        "deterministic_trace": len(digests) == 1 and original.get("trace_digest") in digests,
        "runs": runs,
    }
