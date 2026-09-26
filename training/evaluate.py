"""Keşif modellerini sabit değerlendirme setinde (training/eval_goals.yaml) gerçek sunucuda ölç.

    python training/evaluate.py --model qwen2.5:7b --model qwen3:8b --out artifacts/eval/bench.json

Her hedef için keşif koşulur ve llm_transcript.jsonl'den ölçülür:
  * goal   — hedefin somut sonucu (görev durumu, başarılı komut sayısı, farklı NPC sayısı) 0..1
  * valid  — geçerli oyun komutu oranı (bilinmeyen komut / hatalı argüman / BAD_ARGS hariç)
  * finish — keşfi kendisi `finish` ile bitirdi mi
  * skor   = 0.5*goal + 0.3*valid + 0.2*finish  (hedef başına; model skoru ortalama)
Çıktı JSON'u `training/promote.py` ile "yeni model eskisinden iyi mi" kararında kullanılır.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INVALID_MARKERS = ("Bilinmeyen tool", "BAD_ARGS", "TypeError", "ValueError", "missing a required argument",
                   "geçersiz argüman", "unexpected keyword")


def load_goals(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def read_transcript(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ok(content: Any) -> bool:
    return not (isinstance(content, dict) and content.get("ok") is False)


def score_goal(goal: dict[str, Any], turns: list[dict[str, Any]], out: dict[str, Any]) -> dict[str, Any]:
    from qa.planner.autonomous import META_TOOLS

    calls = invalid = 0
    for t in turns:
        for r in t.get("results", []):
            if r["name"] in META_TOOLS:
                continue
            calls += 1
            text = json.dumps(r.get("content"), ensure_ascii=False)
            if not _ok(r.get("content")) and any(m in text for m in INVALID_MARKERS):
                invalid += 1
    valid = 1.0 - invalid / calls if calls else 0.0
    finished = 1.0 if out.get("stop_reason") == "finished" else 0.0

    chk = goal.get("check") or {}
    goal_score = 0.0
    if chk.get("type") == "quest_state":
        want = set(chk["states"])
        for t in turns:
            for r in t.get("results", []):
                c = r.get("content")
                if isinstance(c, dict):
                    q = ((c.get("quests") or c.get("state", {}).get("quests") or {}) if r["name"] == "observe" else {})
                    blob = json.dumps(c, ensure_ascii=False)
                    if isinstance(q, dict) and (q.get(chk["quest"]) or {}).get("state") in want:
                        goal_score = 1.0
                    elif any(f'"state": "{s}"' in blob for s in want if s != "done") and chk["quest"] in blob:
                        goal_score = max(goal_score, 1.0)
    elif chk.get("type") == "tool_ok":
        n = sum(1 for t in turns for r in t.get("results", []) if r["name"] in chk["tools"] and _ok(r.get("content")))
        goal_score = min(1.0, n / max(1, chk.get("min", 1)))
    elif chk.get("type") == "distinct_args":
        seen = set()
        for t in turns:
            for c, r in zip(t.get("calls", []), t.get("results", [])):
                if c["name"] == chk["tool"] and _ok(r.get("content")):
                    seen.add(tuple(str(c.get("args", {}).get(k)) for k in chk["keys"]))
        goal_score = min(1.0, len(seen) / max(1, chk.get("min", 1)))
    score = round(0.5 * goal_score + 0.3 * valid + 0.2 * finished, 4)
    return {"goal": round(goal_score, 3), "valid": round(valid, 3), "finish": finished, "calls": calls,
            "invalid": invalid, "score": score}


def ollama_provider(model: str, e: Any) -> Any:
    from qa.planner.llm import make_provider

    think = False if model.split(":")[0] in ("qwen3", "deepseek-r1") else e.ollama_think
    return make_provider("ollama", model, base_url=e.ollama_url, num_ctx=e.num_ctx, temperature=e.temperature,
                         think=think)


def run_model(model: str, goals: dict[str, Any], only: list[str] | None, account: str | None,
              explore_fn: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None,
              artifacts_of: Callable[[str], Path | None] | None = None, log: Callable[[str], None] = print,
              should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Modeli hedeflerde koş. explore_fn(goal, provider) -> keşif çıktısı; verilmezse kendi oturumuyla
    (QaService) koşar — 7/24 servis açıkken ajanları atmamak için servis kendi explore_fn'ini verir."""
    from qa.config import load_config

    cfg = load_config()
    e = cfg.explorer
    if explore_fn is None:
        from qa.service import QaService

        svc = QaService(cfg)

        def explore_fn(g: dict[str, Any], provider: Any) -> dict[str, Any]:
            return svc.explore_auto(g["goal"], max_steps=g.get("steps"), setup=g.get("setup") or None,
                                    account=account, provider=provider, validate=False)

        def artifacts_of(run_id: str) -> Path | None:  # noqa: F811
            run = svc.store.get_run(run_id)
            return Path(run["artifacts_dir"]) if run else None
    rows = []
    for g in goals["goals"]:
        if only and g["id"] not in only:
            continue
        if should_stop and should_stop():
            break
        t0 = time.monotonic()
        try:
            out = explore_fn(g, ollama_provider(model, e))
            err = out.get("error")
        except Exception as ex:  # noqa: BLE001 — değerlendirme sürmeli
            out, err = {}, f"{type(ex).__name__}: {ex}"
        dur = round(time.monotonic() - t0, 1)
        adir = artifacts_of(out["run_id"]) if out.get("run_id") and artifacts_of else None
        m = score_goal(g, read_transcript(adir / "llm_transcript.jsonl") if adir else [], out)
        rows.append({"goal_id": g["id"], "run_id": out.get("run_id"), "result": out.get("result"), "error": err,
                     "stop_reason": out.get("stop_reason"), "findings": len(out.get("findings") or []),
                     "seconds": dur, "turns": out.get("turns"), "tokens": (out.get("usage") or {}).get("total_tokens"),
                     **m})
        log(f"  {model:14} {g['id']:14} skor={m['score']:.2f} hedef={m['goal']:.2f} geçerli={m['valid']:.2f} "
            f"bitirdi={int(m['finish'])} {dur}s {err or ''}")
    avg = round(sum(r["score"] for r in rows) / len(rows), 4) if rows else 0.0
    return {"model": model, "score": avg, "goals": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", action="append", required=True, help="Ollama model adı (birden çok verilebilir)")
    ap.add_argument("--goals", default=str(ROOT / "training" / "eval_goals.yaml"))
    ap.add_argument("--only", action="append", help="yalnızca bu hedef kimlik(ler)i")
    ap.add_argument("--account", help="AI_QA_ hesabı (varsayılan: [accounts] default_account)")
    ap.add_argument("--out", default=str(ROOT / "artifacts" / "eval" / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json"))
    a = ap.parse_args()
    goals = load_goals(Path(a.goals))
    results = []
    for model in a.model:
        print(f"== {model}", flush=True)
        results.append(run_model(model, goals, a.only, a.account))
    report = {"set": goals.get("set"), "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "models": results}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nsıralama:")
    for r in sorted(results, key=lambda r: -r["score"]):
        print(f"  {r['model']:16} {r['score']:.3f}")
    print(f"rapor: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
