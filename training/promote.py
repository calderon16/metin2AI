"""Aday modeli sabit değerlendirme setinde mevcut modelle karşılaştır; yalnızca daha iyiyse devreye al.

    python training/promote.py --candidate metin2re-qa:v2 --current metin2re-qa:latest

İki model de training/evaluate.py ile aynı hedeflerde koşulur. Aday, mevcut modelin skorunu en az
--margin kadar geçer VE hiçbir hedefte --max-drop'tan fazla gerilemezse `ollama cp <aday> <mevcut>`
yapılır; keşif ajanı ([explorer] model = "metin2re-qa:latest") bir sonraki işte yeni modeli kullanır.
Karar ve skorlar artifacts/eval/promotions.jsonl'e yazılır. --dry-run yalnızca karar verir.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training.evaluate import load_goals, run_model  # noqa: E402
from training.import_model import ollama_exe  # noqa: E402


def decide(cand: dict, cur: dict | None, margin: float, max_drop: float) -> tuple[bool, str]:
    if cur is None:
        return True, "mevcut model yok; aday ilk model olarak devreye alınır"
    by_goal = {g["goal_id"]: g["score"] for g in cur["goals"]}
    drops = [(g["goal_id"], by_goal.get(g["goal_id"], 0) - g["score"]) for g in cand["goals"]]
    worst = max(drops, key=lambda d: d[1], default=("-", 0.0))
    if cand["score"] < cur["score"] + margin:
        return False, f"aday {cand['score']:.3f} < mevcut {cur['score']:.3f} + {margin}"
    if worst[1] > max_drop:
        return False, f"'{worst[0]}' hedefinde {worst[1]:.2f} geriledi (sınır {max_drop})"
    return True, f"aday {cand['score']:.3f} > mevcut {cur['score']:.3f}"


def model_exists(tag: str) -> bool:
    r = subprocess.run([ollama_exe(), "show", tag], capture_output=True, text=True, encoding="utf-8")
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--current", default="metin2re-qa:latest")
    ap.add_argument("--goals", default=str(ROOT / "training" / "eval_goals.yaml"))
    ap.add_argument("--margin", type=float, default=0.02)
    ap.add_argument("--max-drop", type=float, default=0.25)
    ap.add_argument("--account")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    goals = load_goals(Path(a.goals))
    cand = run_model(a.candidate, goals, None, a.account)
    cur = run_model(a.current, goals, None, a.account) if model_exists(a.current) else None
    ok, why = decide(cand, cur, a.margin, a.max_drop)
    if ok and not a.dry_run:
        subprocess.run([ollama_exe(), "cp", a.candidate, a.current], check=True)
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "set": goals.get("set"), "candidate": a.candidate,
           "current": a.current, "promoted": ok and not a.dry_run, "reason": why,
           "candidate_score": cand["score"], "current_score": cur["score"] if cur else None,
           "detail": {"candidate": cand, "current": cur}}
    log = ROOT / "artifacts" / "eval" / "promotions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(("DEVREYE ALINDI: " if rec["promoted"] else "DEVREYE ALINMADI: ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
