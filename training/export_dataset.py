"""Keşif kayıtlarından (artifacts/…/llm_transcript.jsonl) ince ayar verisi üret.

    python training/export_dataset.py --out training/data --min-score 0.5

Her keşif koşusu, modelin çalışırken gördüğü bağlamla birebir aynı örneklere dönüşür: her asistan turu için
[system] + [ilk kullanıcı mesajı] + (history_turns kadar önceki tur) + hedef asistan cevabı (tool çağrıları).
Yalnızca **tüm çağrıları geçerli** olan asistan turları hedef olur; hatalı çağrılar bağlamda kalır ama
öğretilmez. Çıktı OpenAI/HF sohbet biçimindedir (messages + tools) ve Unsloth/TRL SFTTrainer ile doğrudan
kullanılır (training/notebooks/metin2re_qlora.ipynb).

Gizlilik: yalnızca AI_QA_ hesaplarının koşuları alınır. Gerçek oyuncuların adları "Oyuncu#n" ile değiştirilir,
başkalarının sohbet satırları çıkarılır.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training.evaluate import INVALID_MARKERS  # noqa: E402

QA_PREFIX = "AI_QA_"


def _invalid(content: Any) -> bool:
    if not (isinstance(content, dict) and content.get("ok") is False):
        return False
    text = json.dumps(content, ensure_ascii=False)
    return any(m in text for m in INVALID_MARKERS)


class Scrubber:
    """Gerçek oyuncu adlarını takma adla değiştirir; başka oyuncuların sohbet satırlarını atar."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def learn(self, obj: Any) -> None:
        if isinstance(obj, dict):
            if obj.get("type") == "pc" and isinstance(obj.get("name"), str) and obj["name"] \
                    and not obj["name"].startswith(QA_PREFIX):
                self.names.setdefault(obj["name"], f"Oyuncu#{len(self.names) + 1}")
            for v in obj.values():
                self.learn(v)
        elif isinstance(obj, list):
            for v in obj:
                self.learn(v)

    def text(self, s: str) -> str:
        for real, alias in sorted(self.names.items(), key=lambda kv: -len(kv[0])):
            s = re.sub(rf"\b{re.escape(real)}\b", alias, s)
        return s

    def content(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self.content(v) for k, v in obj.items()}
        if isinstance(obj, list):
            # "Ad : mesaj" biçimli, AI_QA_ olmayan oyuncu sohbetleri
            out = []
            for v in obj:
                t = v.get("text") if isinstance(v, dict) else v
                if isinstance(t, str) and re.match(r"^\s*(?!AI_QA_)[^\s:]{2,24} : ", t):
                    continue
                out.append(self.content(v))
            return out
        if isinstance(obj, str):
            return self.text(obj)
        return obj


def _tools_schema() -> list[dict[str, Any]]:
    from qa.planner.autonomous import behaviour_tools, meta_tools

    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in behaviour_tools() + meta_tools()]


def _assistant(turn: dict[str, Any], scrub: Scrubber) -> dict[str, Any]:
    return {"role": "assistant", "content": scrub.text(turn.get("text") or ""),
            "tool_calls": [{"type": "function", "function": {"name": c["name"],
                                                             "arguments": scrub.content(c.get("args") or {})}}
                           for c in turn.get("calls", [])]}


def _tool_msgs(turn: dict[str, Any], scrub: Scrubber) -> list[dict[str, Any]]:
    msgs = [{"role": "tool", "name": r["name"],
             "content": json.dumps(scrub.content(r.get("content")), ensure_ascii=False)} for r in turn.get("results", [])]
    if turn.get("note"):
        msgs.append({"role": "user", "content": turn["note"]})
    return msgs


def samples_from_run(run: dict[str, Any], turns: list[dict[str, Any]], report: dict[str, Any]) -> list[dict[str, Any]]:
    from qa.planner.autonomous import SYSTEM_PROMPT

    head = turns[0] if turns and turns[0].get("turn") == 0 else None
    body = [t for t in turns if t.get("turn", 0) > 0]
    if not body:
        return []
    scrub = Scrubber()
    scrub.learn(turns)
    approx = head is None
    if head is None:
        goal = report.get("goal") or ""
        head = {"system": SYSTEM_PROMPT.format(max_steps=report.get("steps") or 40),
                "user": f"TEST HEDEFİ:\n{goal}\n\nPlanını kısaca düşün, sonra tool çağrılarıyla test etmeye başla.",
                "goal": goal, "history_turns": 10, "model": (report.get("llm") or {}).get("model")}
    keep = int(head.get("history_turns") or 10)
    system = {"role": "system", "content": head["system"]}
    first = {"role": "user", "content": scrub.text(head["user"])}
    out = []
    for i, turn in enumerate(body):
        if not turn.get("calls") or any(_invalid(r.get("content")) for r in turn.get("results", [])):
            continue
        prev = body[max(0, i - keep):i]
        ctx: list[dict[str, Any]] = []
        for p in prev:
            ctx.append(_assistant(p, scrub))
            ctx.extend(_tool_msgs(p, scrub))
        out.append({"messages": [system, first, *ctx, _assistant(turn, scrub)],
                    "meta": {"run_id": run["run_id"], "turn": turn["turn"], "goal": head.get("goal"),
                             "teacher": head.get("model"), "approx_context": approx}})
    return out


def iter_explore_runs(store: Any) -> list[dict[str, Any]]:
    rows = store.query("SELECT * FROM runs WHERE mode = 'explore' ORDER BY started_at", ())
    return [r for r in rows if str(r.get("account") or r.get("player") or "").startswith(QA_PREFIX)]


def export(out_dir: Path, min_score: float = 0.5, val_ratio: float = 0.1, seed: int = 7) -> dict[str, Any]:
    from qa.config import load_config
    from qa.store.db import Store
    from training.evaluate import read_transcript, score_goal

    cfg = load_config()
    store = Store(cfg.db_file)
    runs = iter_explore_runs(store)
    kept, skipped, samples = 0, 0, []
    for run in runs:
        tpath = Path(run["artifacts_dir"]) / "llm_transcript.jsonl"
        rpath = Path(run["artifacts_dir"]) / "report.json"
        turns = read_transcript(tpath)
        report = json.loads(rpath.read_text(encoding="utf-8")) if rpath.exists() else {}
        if not turns or report.get("result") == "ERROR":
            skipped += 1
            continue
        q = score_goal({}, turns, {"stop_reason": report.get("stop_reason")})
        # Hedef ölçütü koşuya özgü olmadığından burada yalnızca geçerlilik + bitirme kullanılır
        quality = round((0.6 * q["valid"] + 0.4 * q["finish"]), 3)
        if quality < min_score:
            skipped += 1
            continue
        s = samples_from_run(run, turns, report)
        for x in s:
            x["meta"]["quality"] = quality
        samples += s
        kept += 1

    # Aynı koşudan gelen örnekler aynı bölmede kalsın (sızıntı olmasın)
    rng = random.Random(seed)
    run_ids = sorted({x["meta"]["run_id"] for x in samples})
    rng.shuffle(run_ids)
    n_val = max(1, int(len(run_ids) * val_ratio)) if len(run_ids) > 1 else 0
    val_ids = set(run_ids[:n_val])
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tools = _tools_schema()
    for name, part in (("train", [x for x in samples if x["meta"]["run_id"] not in val_ids]),
                       ("val", [x for x in samples if x["meta"]["run_id"] in val_ids])):
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for x in part:
                f.write(json.dumps({**x, "tools": tools}, ensure_ascii=False) + "\n")
    digest = hashlib.sha256((out / "train.jsonl").read_bytes()).hexdigest()[:12]
    manifest = {"runs_kept": kept, "runs_skipped": skipped, "samples": len(samples),
                "train_runs": len(run_ids) - n_val, "val_runs": n_val, "train_sha": digest,
                "min_score": min_score, "tools": [t["function"]["name"] for t in tools]}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "training" / "data"))
    ap.add_argument("--min-score", type=float, default=0.5, help="koşunun kalite skoru bu değerin altındaysa alınmaz")
    ap.add_argument("--val-ratio", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    print(json.dumps(export(Path(a.out), a.min_score, a.val_ratio, a.seed), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
