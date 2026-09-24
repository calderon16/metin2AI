"""Senaryo dosyalarını yükleme/kaydetme."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .schema import Scenario

NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")


class ScenarioError(Exception):
    pass


def parse_scenario(text: str) -> Scenario:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ScenarioError(f"YAML hatası: {e}") from e
    if not isinstance(data, dict):
        raise ScenarioError("Senaryo bir YAML sözlüğü olmalı")
    try:
        return Scenario.model_validate(data)
    except ValidationError as e:
        raise ScenarioError(f"Senaryo doğrulanamadı:\n{e}") from e


def dump_scenario(sc: Scenario) -> str:
    return yaml.safe_dump(sc.to_yaml_dict(), allow_unicode=True, sort_keys=False)


def scenario_path(directory: Path, name: str) -> Path:
    if not NAME_RE.match(name):
        raise ScenarioError(f"Geçersiz senaryo adı: {name!r}")
    return directory / f"{name}.yaml"


def load_scenario(directory: Path, name: str) -> tuple[Scenario, str]:
    p = scenario_path(directory, name)
    if not p.exists():
        raise ScenarioError(f"Senaryo bulunamadı: {name} ({p})")
    text = p.read_text(encoding="utf-8")
    return parse_scenario(text), text


def list_scenarios(directory: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(directory.glob("*.yaml")):
        try:
            sc = parse_scenario(p.read_text(encoding="utf-8"))
            out.append({"name": sc.name, "file": p.name, "description": sc.description.strip(), "tags": sc.tags,
                        "steps": len(sc.steps)})
        except ScenarioError as e:
            out.append({"name": p.stem, "file": p.name, "error": str(e)})
    return out


def save_scenario(directory: Path, name: str, text: str, overwrite: bool = False) -> Path:
    p = scenario_path(directory, name)
    sc = parse_scenario(text)
    if sc.name != name:
        raise ScenarioError(f"Dosya adı ({name}) ile senaryo 'name' alanı ({sc.name}) aynı olmalı")
    if p.exists() and not overwrite:
        raise ScenarioError(f"{p.name} zaten var (overwrite=true ile üzerine yaz)")
    directory.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p
