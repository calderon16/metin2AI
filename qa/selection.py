"""Değişikliğe göre test seçimi.

Claude `shop.cpp`'yi değiştirdiyse bütün suite yerine dükkanla ilgili senaryolar çalışsın:

  1. Değişen dosyalar: verilen liste ya da `git diff` (kaynak repo, base'e göre + izlenmeyenler)
  2. Her dosya için:
       * senaryo dosyasının kendisi değiştiyse → o senaryo
       * senaryonun `covers:` kalıplarından biri eşleşirse → o senaryo
       * qa.toml [selection.rules] kalıbı eşleşirse → kuralın hedefleri (etiket / scenario:ad / *)
       * hiçbiri eşleşmezse → [selection].fallback
  3. [selection].always her zaman eklenir.

Sonuç her senaryo için "neden seçildi" listesini içerir; Claude hangi değişikliğin hangi testi
tetiklediğini görür.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .config import QaConfig


class SelectionError(Exception):
    pass


def _norm(path: str) -> str:
    return str(PurePosixPath(path.replace("\\", "/"))).removeprefix("./")


def match(pattern: str, path: str) -> bool:
    p = _norm(path)
    pat = pattern.replace("\\", "/")
    if "/" not in pat:
        return fnmatch.fnmatch(PurePosixPath(p).name, pat)
    return fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, "*/" + pat)


def git_changed_files(repo: Path, base: str = "HEAD") -> list[str]:
    def git(*args: str) -> list[str]:
        try:
            out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SelectionError(f"git çalıştırılamadı: {e}") from e
        if out.returncode != 0:
            raise SelectionError(f"git {' '.join(args)}: {out.stderr.strip()}")
        return [l for l in out.stdout.splitlines() if l.strip()]

    files = git("diff", "--name-only", base)
    files += git("ls-files", "--others", "--exclude-standard")
    return sorted(set(files))


def select(cfg: QaConfig, changed: list[str], scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {s["name"]: s for s in scenarios if "error" not in s}
    names = sorted(by_name)
    reasons: dict[str, list[str]] = {}
    unmatched: list[str] = []
    scen_dir = _norm(str(cfg.scenarios_dir))

    def add_target(target: str, why: str) -> None:
        if target == "*":
            hit = names
        elif target.startswith("scenario:"):
            hit = [target.split(":", 1)[1]] if target.split(":", 1)[1] in by_name else []
        else:
            tag = target[4:] if target.startswith("tag:") else target
            hit = [n for n in names if tag in by_name[n].get("tags", [])]
        for n in hit:
            reasons.setdefault(n, [])
            if why not in reasons[n]:
                reasons[n].append(why)

    for f in changed:
        nf = _norm(f)
        matched = False
        p = PurePosixPath(nf)
        if p.suffix in {".yaml", ".yml"} and (str(p.parent).endswith(scen_dir) or p.parent.name == "scenarios"):
            if p.stem in by_name:
                add_target(f"scenario:{p.stem}", f"{nf} (senaryo dosyası)")
                matched = True
        for n in names:
            for pat in by_name[n].get("covers", []):
                if match(pat, nf):
                    add_target(f"scenario:{n}", f"{nf} ~ covers '{pat}'")
                    matched = True
        for pat, targets in cfg.selection.rules.items():
            if match(pat, nf):
                for t in targets:
                    add_target(t, f"{nf} ~ kural '{pat}' → {t}")
                matched = True
        if not matched:
            unmatched.append(nf)

    if unmatched:
        for t in cfg.selection.fallback:
            add_target(t, f"eşleşmeyen değişiklik ({len(unmatched)} dosya) → fallback {t}")
    if changed:
        for t in cfg.selection.always:
            add_target(t, f"always → {t}")

    selected = sorted(reasons)
    return {
        "changed_files": [_norm(f) for f in changed],
        "unmatched_files": unmatched,
        "selected": selected,
        "reasons": {n: reasons[n] for n in selected},
        "skipped": [n for n in names if n not in reasons],
    }
