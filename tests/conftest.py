from pathlib import Path

import pytest

from qa.config import QaConfig
from qa.scenario.runner import ScenarioRunner
from qa.service import QaService
from qa.store.db import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path: Path) -> QaConfig:
    c = QaConfig(root=ROOT, artifacts_dir=tmp_path / "artifacts", db_path=tmp_path / "qa.sqlite")
    return c


@pytest.fixture
def runner(cfg: QaConfig) -> ScenarioRunner:
    return ScenarioRunner(cfg, Store(cfg.db_file))


@pytest.fixture
def service(cfg: QaConfig, tmp_path: Path) -> QaService:
    # Senaryo yazma testleri repo'yu kirletmesin diye senaryoları geçici klasöre kopyala
    sdir = tmp_path / "scenarios"
    sdir.mkdir()
    for p in (ROOT / "scenarios").glob("*.yaml"):
        (sdir / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    cfg.scenarios_dir = sdir
    return QaService(cfg)
