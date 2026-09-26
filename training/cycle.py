"""Kendini geliştiren öğrenme döngüsü: veri → eğitim → Ollama → değerlendirme → (daha iyiyse) devreye alma.

7/24 servisin `learning_cycle` işi bu modülü çalıştırır (değerlendirme servisin kiraladığı ajanla yapılır);
elle de çalıştırılabilir (servis kapalıyken):

    python training/cycle.py [--gguf dosya.gguf]

Eğitim adımı sırası: --gguf verilmişse o dosya; training/incoming/ altında henüz yüklenmemiş yeni bir GGUF
varsa o; Kaggle kimliği varsa Kaggle'da otomatik eğitim; hiçbiri yoksa "eğitim bekleniyor" ile durur
(veri training/data/<tarih>/ altında hazırdır; not defterini elle çalıştırıp GGUF'u training/incoming/'a koyun).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INCOMING = ROOT / "training" / "incoming"
STATE = ROOT / "artifacts" / "eval" / "cycle_state.json"
CURRENT = "metin2re-qa:latest"


def _state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def _new_gguf(st: dict[str, Any]) -> Path | None:
    done = set(st.get("imported", []))
    files = sorted(INCOMING.glob("*.gguf"), key=lambda p: p.stat().st_mtime) if INCOMING.exists() else []
    fresh = [p for p in files if f"{p.name}:{p.stat().st_size}:{int(p.stat().st_mtime)}" not in done]
    return fresh[-1] if fresh else None


def _mark_imported(st: dict[str, Any], p: Path) -> None:
    st.setdefault("imported", []).append(f"{p.name}:{p.stat().st_size}:{int(p.stat().st_mtime)}")


def run_cycle(params: dict[str, Any] | None = None, explore_fn: Callable[..., dict[str, Any]] | None = None,
              artifacts_of: Callable[[str], Path | None] | None = None, log: Callable[[str], None] = print,
              should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    from training import kaggle_train
    from training.evaluate import load_goals, run_model
    from training.export_dataset import export
    from training.import_model import import_gguf
    from training.promote import decide, model_exists

    p = params or {}
    base_ollama = p.get("base_ollama", "qwen3:8b")
    min_samples = int(p.get("min_samples", 150))
    stamp = time.strftime("%Y%m%d-%H%M")
    st = _state()
    out: dict[str, Any] = {"stamp": stamp}

    # 1) veri
    data_dir = ROOT / "training" / "data" / stamp
    man = export(data_dir, float(p.get("min_score", 0.5)))
    out["dataset"] = {"dir": str(data_dir), **{k: man[k] for k in ("samples", "runs_kept", "train_runs", "val_runs")}}
    log(f"veri: {man['samples']} örnek, {man['runs_kept']} koşu")

    # 2) eğitim (GGUF)
    gguf = Path(p["gguf"]) if p.get("gguf") else _new_gguf(st)
    if gguf is None:
        if man["samples"] < min_samples:
            out["status"] = "collecting"
            out["message"] = f"Eğitim için veri az ({man['samples']}/{min_samples} örnek); keşifler veri toplamaya devam ediyor"
            return out
        if kaggle_train.credentials_available():
            log("Kaggle'da eğitim başlıyor")
            try:
                gguf = kaggle_train.train(data_dir, INCOMING, p.get("base_unsloth"), log=log, should_stop=should_stop)
            except kaggle_train.KaggleError as e:
                out["status"], out["message"] = "training_failed", str(e)
                return out
        else:
            out["status"] = "waiting_for_training"
            out["message"] = (f"Veri hazır: {data_dir}. Kaggle hesabı bağlı değil: not defterini elle çalıştırıp GGUF'u "
                              f"{INCOMING} klasörüne koyun ya da KAGGLE_USERNAME/KAGGLE_KEY ayarlayın.")
            return out

    # 3) Ollama'ya ekle
    tag = f"metin2re-qa:{stamp}"
    log(f"Ollama'ya ekleniyor: {gguf.name} → {tag}")
    import_gguf(gguf, base_ollama, tag)
    _mark_imported(st, gguf)
    _save_state(st)
    out["candidate"] = tag

    # 4) değerlendir ve karar ver
    goals = load_goals(ROOT / "training" / "eval_goals.yaml")
    current = CURRENT if model_exists(CURRENT) else base_ollama
    cand = run_model(tag, goals, None, None, explore_fn, artifacts_of, log, should_stop)
    cur = run_model(current, goals, None, None, explore_fn, artifacts_of, log, should_stop)
    ok, why = decide(cand, cur, float(p.get("margin", 0.02)), float(p.get("max_drop", 0.25)))
    if ok:
        from training.import_model import ollama_exe
        import subprocess

        subprocess.run([ollama_exe(), "cp", tag, CURRENT], check=True, capture_output=True)
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "set": goals.get("set"), "candidate": tag, "current": current,
           "promoted": ok, "reason": why, "candidate_score": cand["score"], "current_score": cur["score"],
           "dataset": out["dataset"], "detail": {"candidate": cand, "current": cur}}
    log_path = ROOT / "artifacts" / "eval" / "promotions.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out.update(status="promoted" if ok else "kept_current", message=why,
               scores={"candidate": cand["score"], "current": cur["score"]})
    log(("DEVREYE ALINDI: " if ok else "DEVREYE ALINMADI: ") + why)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gguf", help="eğitilmiş GGUF dosyası (yoksa incoming/Kaggle)")
    ap.add_argument("--base-ollama", default="qwen3:8b")
    ap.add_argument("--min-samples", type=int, default=150)
    a = ap.parse_args()
    res = run_cycle({"gguf": a.gguf, "base_ollama": a.base_ollama, "min_samples": a.min_samples})
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
