"""Claude'un okuyacağı yapılandırılmış test raporu."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def git_info(repo: Path) -> dict[str, str | None]:
    def run(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() or None if out.returncode == 0 else None

    return {
        "git_commit": run("rev-parse", "--short", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": None if run("rev-parse", "HEAD") is None else bool(run("status", "--porcelain")),
    }


def failure_signature(report: dict[str, Any]) -> str | None:
    """Replay karşılaştırması için hatanın kimliği: ilk hatanın adımı + türü + adı."""
    f = report.get("failure")
    if not f:
        return None
    return f"step={f.get('step')}|{f.get('kind')}|{f.get('name')}"


def summarize(report: dict[str, Any]) -> str:
    r = report["result"]
    base = f"{report['run_id']} {report['scenario']} {r} (seed={report['seed']})"
    f = report.get("failure")
    if not f:
        return base
    if f.get("step") is not None:
        where = f"adım {f['step']} ({f.get('action')})"
    else:
        where = "örtük oracle kontrolü" if f.get("kind") == "oracle" else "final assert"
    extra = f" (+{len(report['failures']) - 1} hata daha)" if len(report["failures"]) > 1 else ""
    return f"{base}: {where} — {f.get('name')}: {f.get('message') or ''} " \
           f"beklenen={f.get('expected')} gerçekleşen={f.get('actual')}{extra}"
