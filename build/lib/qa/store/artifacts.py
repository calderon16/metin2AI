"""Run artifact'leri: artifacts/<run_id>/{report.json, trace.jsonl, trace.txt, scenario.yaml,
client.log, server.log, server_events.jsonl, screenshots/*.png}"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SAFE = re.compile(r"[^A-Za-z0-9_\-]+")


class RunArtifacts:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._shots = 0

    def path(self, name: str) -> Path:
        return self.dir / name

    def write_text(self, name: str, text: str) -> str:
        self.path(name).write_text(text, encoding="utf-8")
        return name

    def write_json(self, name: str, data: Any) -> str:
        self.path(name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return name

    def save_screenshot(self, label: str, data: bytes, ext: str = "png") -> str:
        if ext not in {"png", "jpg", "jpeg", "bmp"}:
            ext = "png"
        self._shots += 1
        d = self.dir / "screenshots"
        d.mkdir(exist_ok=True)
        name = f"{self._shots:03d}_{_SAFE.sub('_', label)[:40]}.{ext}"
        (d / name).write_bytes(data)
        return f"screenshots/{name}"

    def screenshots(self) -> list[str]:
        d = self.dir / "screenshots"
        return sorted(f"screenshots/{p.name}" for p in d.iterdir() if p.is_file()) if d.exists() else []


def read_artifact(run_dir: Path, name: str) -> Path:
    """Run klasörü dışına çıkılmasını engelleyerek artifact yolunu çözer."""
    p = (run_dir / name).resolve()
    if run_dir.resolve() not in p.parents and p != run_dir.resolve():
        raise ValueError("Geçersiz artifact yolu")
    if not p.exists():
        raise FileNotFoundError(name)
    return p
