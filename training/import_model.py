"""Eğitilen GGUF modelini Ollama'ya ekle (temel modelin sohbet/tool şablonuyla).

    python training/import_model.py metin2re-qa.Q4_K_M.gguf --base qwen2.5:7b --tag metin2re-qa:v1

Temel modelin Modelfile'ından TEMPLATE, PARAMETER ve SYSTEM satırları alınır, yalnızca FROM yeni GGUF'a
çevrilir. Böylece Ollama'nın tool çağrısı biçimi (ör. Qwen'in <tool_call>) eğitimdekiyle aynı kalır.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def ollama_exe() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if local.exists():
        return str(local)
    raise SystemExit("ollama bulunamadı (https://ollama.com)")


def base_modelfile(base: str) -> str:
    out = subprocess.run([ollama_exe(), "show", base, "--modelfile"], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return out


def build_modelfile(base_text: str, gguf: Path) -> str:
    """FROM satırını değiştir; yorumları at; TEMPLATE/PARAMETER/SYSTEM/LICENSE bloklarını koru."""
    lines = [ln for ln in base_text.splitlines() if not ln.startswith("#")]
    text = "\n".join(lines)
    text = re.sub(r"(?m)^FROM .*$", f"FROM {gguf.resolve().as_posix()}", text, count=1)
    # LICENSE blokları çok uzun olabilir; model kullanımını etkilemez
    text = re.sub(r'(?s)LICENSE """.*?"""\n?', "", text)
    return text.strip() + "\n"


def import_gguf(gguf: Path, base: str, tag: str, num_ctx: int = 12288) -> str:
    if not gguf.exists():
        raise FileNotFoundError(f"GGUF yok: {gguf}")
    mf = build_modelfile(base_modelfile(base), gguf)
    if "num_ctx" not in mf:
        mf += f"PARAMETER num_ctx {num_ctx}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".Modelfile", delete=False, encoding="utf-8") as f:
        f.write(mf)
        path = f.name
    try:
        subprocess.run([ollama_exe(), "create", tag, "-f", path], check=True, capture_output=True)
    finally:
        os.unlink(path)
    return tag


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gguf", type=Path)
    ap.add_argument("--base", default="qwen2.5:7b", help="eğitimin temel aldığı Ollama modeli (şablon için)")
    ap.add_argument("--tag", default="metin2re-qa:candidate")
    ap.add_argument("--num-ctx", type=int, default=12288)
    a = ap.parse_args()
    import_gguf(a.gguf, a.base, a.tag, a.num_ctx)
    print(f"Ollama modeli hazır: {a.tag}  (sına: python training/evaluate.py --model {a.tag})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
