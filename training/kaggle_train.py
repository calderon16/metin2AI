"""Kaggle'ın ücretsiz GPU'sunda eğitimi otomatik çalıştır (kaggle CLI ile).

Kimlik bilgisi dosyaya YAZILMAZ; Kaggle'ın standart yerlerinden okunur: KAGGLE_USERNAME + KAGGLE_KEY
ortam değişkenleri ya da %USERPROFILE%\\.kaggle\\kaggle.json (Kaggle → Settings → API → Create New Token).

    python training/kaggle_train.py training/data --out training/incoming

Adımlar: (1) veri kümesini özel "metin2re-qa-data" olarak yükle/sürümle, (2) not defterini GPU'lu özel
kernel olarak gönder, (3) bitene kadar bekle, (4) çıktıdaki GGUF'u indir.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "training" / "notebooks" / "metin2re_qlora.ipynb"
DATASET_SLUG = "metin2re-qa-data"
KERNEL_SLUG = "metin2re-qa-train"


class KaggleError(Exception):
    pass


def kaggle_cli() -> list[str]:
    exe = shutil.which("kaggle") or str(Path(sys.executable).with_name("kaggle.exe"))
    if Path(exe).exists() or shutil.which("kaggle"):
        return [exe]
    return [sys.executable, "-m", "kaggle"]


def credentials_available() -> bool:
    if os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"):
        return True
    return (Path.home() / ".kaggle" / "kaggle.json").exists()


def username() -> str:
    if os.environ.get("KAGGLE_USERNAME"):
        return os.environ["KAGGLE_USERNAME"]
    p = Path.home() / ".kaggle" / "kaggle.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))["username"]
    raise KaggleError("Kaggle kimlik bilgisi yok (training/README.md → Kaggle hesabı)")


def _run(args: list[str], log: Callable[[str], None]) -> str:
    r = subprocess.run(kaggle_cli() + args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise KaggleError(f"kaggle {' '.join(args[:2])} başarısız: {out.strip()[-500:]}")
    log(out.strip()[-300:])
    return out


def push_dataset(data_dir: Path, log: Callable[[str], None] = print) -> str:
    user = username()
    meta = {"title": DATASET_SLUG, "id": f"{user}/{DATASET_SLUG}", "licenses": [{"name": "CC0-1.0"}]}
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("train.jsonl", "val.jsonl", "manifest.json"):
            shutil.copy2(data_dir / name, Path(tmp) / name)
        (Path(tmp) / "dataset-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        listing = subprocess.run(kaggle_cli() + ["datasets", "status", meta["id"]], capture_output=True, text=True)
        if listing.returncode == 0 and "ready" in (listing.stdout or "").lower():
            _run(["datasets", "version", "-p", tmp, "-m", time.strftime("veri %Y-%m-%d %H:%M"), "-d"], log)
        else:
            _run(["datasets", "create", "-p", tmp], log)       # varsayılan: özel (private)
    return meta["id"]


def push_kernel(dataset_id: str, base_model: str | None, log: Callable[[str], None] = print) -> str:
    user = username()
    kid = f"{user}/{KERNEL_SLUG}"
    with tempfile.TemporaryDirectory() as tmp:
        nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        if base_model:
            for c in nb["cells"]:
                c["source"] = [ln.replace('BASE_MODEL = "unsloth/Qwen2.5-7B-Instruct-bnb-4bit"',
                                          f'BASE_MODEL = "{base_model}"') for ln in c["source"]]
        (Path(tmp) / NOTEBOOK.name).write_text(json.dumps(nb, ensure_ascii=False), encoding="utf-8")
        meta = {"id": kid, "title": KERNEL_SLUG, "code_file": NOTEBOOK.name, "language": "python",
                "kernel_type": "notebook", "is_private": True, "enable_gpu": True, "enable_internet": True,
                "dataset_sources": [dataset_id], "competition_sources": [], "kernel_sources": []}
        (Path(tmp) / "kernel-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        _run(["kernels", "push", "-p", tmp], log)
    return kid


def wait_kernel(kid: str, timeout_s: float = 4 * 3600, poll_s: float = 60, log: Callable[[str], None] = print,
                should_stop: Callable[[], bool] | None = None) -> None:
    t0 = time.monotonic()
    while True:
        if should_stop and should_stop():
            raise KaggleError("İptal edildi")
        out = subprocess.run(kaggle_cli() + ["kernels", "status", kid], capture_output=True, text=True).stdout or ""
        low = out.lower()
        if "complete" in low:
            return
        if "error" in low or "cancel" in low:
            raise KaggleError(f"Kaggle eğitimi başarısız: {out.strip()} (kernel günlüğü: kaggle.com/code/{kid})")
        if time.monotonic() - t0 > timeout_s:
            raise KaggleError("Kaggle eğitimi zaman aşımı")
        log(f"eğitim sürüyor: {out.strip()[-120:]}")
        time.sleep(poll_s)


def download_gguf(kid: str, out_dir: Path, log: Callable[[str], None] = print) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    _run(["kernels", "output", kid, "-p", str(out_dir)], log)
    ggufs = sorted(out_dir.rglob("*.gguf"), key=lambda p: p.stat().st_mtime)
    if not ggufs:
        raise KaggleError("Kaggle çıktısında GGUF yok (not defterinin son hücrelerini kontrol edin)")
    return ggufs[-1]


def train(data_dir: Path, out_dir: Path, base_model: str | None = None, log: Callable[[str], None] = print,
          should_stop: Callable[[], bool] | None = None) -> Path:
    if not credentials_available():
        raise KaggleError("Kaggle kimlik bilgisi yok (training/README.md → Kaggle hesabı)")
    ds = push_dataset(data_dir, log)
    time.sleep(30)                 # veri kümesi işlenirken kernel eski sürümü görmesin
    kid = push_kernel(ds, base_model, log)
    wait_kernel(kid, log=log, should_stop=should_stop)
    return download_gguf(kid, out_dir, log)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "training" / "incoming")
    ap.add_argument("--base-model", help="Unsloth temel modeli (not defterindeki BASE_MODEL'in yerine)")
    a = ap.parse_args()
    print("GGUF:", train(a.data_dir, a.out, a.base_model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
