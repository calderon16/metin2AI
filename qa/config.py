"""QA yapılandırması (qa.toml).

Dosya bulunamazsa varsayılanlar kullanılır: in-process simülatör, ./scenarios, ./artifacts.
Yol çözümü: QA_CONFIG ortam değişkeni > ./qa.local.toml > ./qa.toml.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class BridgeConfig:
    # "sim": in-process simülatör, "tcp": QA client (veya `metin2-qa sim-server`) TCP bağlantısı
    mode: str = "sim"
    host: str = "127.0.0.1"
    port: int = 47800
    timeout_s: float = 15.0


@dataclass
class SimConfig:
    # Bilerek hata enjekte etmek için (ör. "no_drop", "quest_off_by_one"); bkz. qa/sim/world.py FAULTS
    faults: list[str] = field(default_factory=list)


@dataclass
class ServerConfig:
    # Liste halinde komutlar; shell kullanılmaz. Boşsa tool "yapılandırılmamış" döner.
    start: list[str] = field(default_factory=list)
    stop: list[str] = field(default_factory=list)
    cwd: str | None = None
    # QA_EVENT/QA_ASSERT çıktısı (integration/server/qa_event.cpp) — boşsa bridge'den okunur
    events_file: str | None = None
    # İsim -> dosya yolu (syserr, syslog ...). Run başındaki boyuttan itibaren eklenen satırlar toplanır.
    log_files: dict[str, str] = field(default_factory=dict)
    error_patterns: list[str] = field(default_factory=lambda: ["SYSERR", "Traceback", "QUEST_ERROR", "SQL error"])


@dataclass
class BuildConfig:
    # hedef adı -> komut listesi, ör. {"server": ["gmake", "-C", "/src/server/game/src", "-j8"]}
    commands: dict[str, list[str]] = field(default_factory=dict)
    cwd: str | None = None
    timeout_s: int = 3600


# Kaynak dosya kalıbı -> çalıştırılacak senaryo hedefleri.
# Kalıpta "/" yoksa dosya adına, varsa tam yola (fnmatch, * dizinleri de kapsar) bakılır.
# Hedef: etiket ("shop"), "scenario:<ad>" veya "*" (tüm senaryolar).
DEFAULT_SELECTION_RULES: dict[str, list[str]] = {
    # Sunucu (game/src) — klasik dosya adları
    "shop*.cpp": ["shop"], "shop*.h": ["shop"],
    "exchange*.cpp": ["trade"], "exchange*.h": ["trade"],
    "party*.cpp": ["party"], "party*.h": ["party"],
    "char_item.cpp": ["item", "inventory"], "item*.cpp": ["item"], "item*.h": ["item"],
    "char_battle.cpp": ["combat"], "battle*.cpp": ["combat"], "mob_manager*.cpp": ["combat", "drop"],
    "char_skill.cpp": ["combat"], "*drop*": ["drop"],
    "quest*.cpp": ["quest"], "questlua*.cpp": ["quest"], "*.quest": ["quest"],
    "input_main.cpp": ["shop", "item", "trade"], "input_login.cpp": ["smoke"],
    "char.cpp": ["*"], "char.h": ["*"], "packet*.h": ["*"],
    # Client root
    "uiQuest.py": ["quest"], "uiShop.py": ["shop"], "uiExchange.py": ["trade"], "uiParty.py": ["party"],
    "uiInventory.py": ["inventory", "item"], "game.py": ["*"], "intro*.py": ["smoke"],
    # Bu repo
    "qa/*": ["*"], "integration/*": ["*"], ".github/*": ["*"], "pyproject.toml": ["*"],
    # Test gerektirmeyen dosyalar (boş hedef = eşleşti ama senaryo yok)
    "*.md": [], "*.txt": [], "docs/*": [],
}


@dataclass
class SelectionConfig:
    rules: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_SELECTION_RULES))
    # Hiçbir kurala uymayan değişiklik varsa çalışacak hedefler
    fallback: list[str] = field(default_factory=lambda: ["smoke"])
    # Her seçime eklenen hedefler
    always: list[str] = field(default_factory=lambda: ["smoke"])
    # true ise qa.toml'daki rules varsayılanların üzerine eklenir; false ise yerini alır
    extend_defaults: bool = True


@dataclass
class AccountsConfig:
    allowed_prefix: str = "AI_QA_"
    default_account: str = "AI_QA_001"
    # Şifre dosyada değil ortam değişkeninde tutulabilir: QA_ACCOUNT_PASSWORD
    password: str = "qa"


@dataclass
class QaConfig:
    env: str = "qa"
    root: Path = field(default_factory=Path.cwd)
    scenarios_dir: Path = Path("scenarios")
    artifacts_dir: Path = Path("artifacts")
    db_path: Path = Path("artifacts/qa.sqlite")
    # Build bilgisinin (commit/branch) okunacağı Metin2 kaynak reposu
    source_repo: Path = Path(".")
    default_seed: int | None = None
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    accounts: AccountsConfig = field(default_factory=AccountsConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)

    def resolve(self, p: Path | str) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (self.root / p)

    @property
    def scenarios_path(self) -> Path:
        return self.resolve(self.scenarios_dir)

    @property
    def artifacts_path(self) -> Path:
        return self.resolve(self.artifacts_dir)

    @property
    def db_file(self) -> Path:
        return self.resolve(self.db_path)

    def check_environment(self) -> None:
        """Production ortamında hiçbir QA işlemine izin verilmez."""
        if self.env.lower() in {"prod", "production", "live"}:
            raise ConfigError(f"QA sistemi '{self.env}' ortamında çalıştırılamaz (yalnızca dev/qa/staging).")

    def check_account(self, name: str) -> None:
        if not name.startswith(self.accounts.allowed_prefix):
            raise ConfigError(
                f"'{name}' bir QA hesabı değil; yalnızca '{self.accounts.allowed_prefix}*' hesapları kullanılabilir."
            )


def _apply(obj, data: dict, section: str) -> None:
    for key, value in data.items():
        if not hasattr(obj, key):
            raise ConfigError(f"Bilinmeyen ayar: [{section}] {key}")
        setattr(obj, key, value)


def find_config_file() -> Path | None:
    env_path = os.environ.get("QA_CONFIG")
    if env_path:
        return Path(env_path)
    for name in ("qa.local.toml", "qa.toml"):
        p = Path.cwd() / name
        if p.exists():
            return p
    return None


def load_config(path: Path | str | None = None) -> QaConfig:
    cfg = QaConfig()
    path = Path(path) if path else find_config_file()
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Yapılandırma dosyası bulunamadı: {path}")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        cfg.root = path.resolve().parent
        sections = {
            "bridge": cfg.bridge,
            "sim": cfg.sim,
            "server": cfg.server,
            "build": cfg.build,
            "accounts": cfg.accounts,
            "selection": cfg.selection,
        }
        for key, value in data.items():
            if key in sections:
                _apply(sections[key], value, key)
            elif key in {"scenarios_dir", "artifacts_dir", "db_path", "source_repo"}:
                setattr(cfg, key, Path(value))
            elif key in {"env", "default_seed"}:
                setattr(cfg, key, value)
            else:
                raise ConfigError(f"Bilinmeyen ayar: {key}")
    if path is not None:
        sel = data.get("selection", {})
        if "rules" in sel and cfg.selection.extend_defaults:
            cfg.selection.rules = {**DEFAULT_SELECTION_RULES, **sel["rules"]}
    pw = os.environ.get("QA_ACCOUNT_PASSWORD")
    if pw:
        cfg.accounts.password = pw
    return cfg
