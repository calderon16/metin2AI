"""YAML senaryo modeli.

    name: kill_mob_pickup
    account: AI_QA_001
    seed: 846219
    setup:
      - set_level: 20
      - give_item: {vnum: 27001, count: 5}
    steps:
      - walk_to: {x: 8000, y: 8000}
      - kill_monster: {vnum: 101, count: 3}
        expect:
          - server_event: {name: MOB_KILL, min: 3}
      - pickup: {vnum: 30000, all: true}
    assert:
      - inventory_contains: {vnum: 30000}
      - server_errors: 0

Adım/assert biçimleri: `- adim` (parametresiz), `- adim: deger` (ilk parametreye bağlanır),
`- adim: {param: deger}`. Adımlara `expect` (satır içi assert), `expect_error` (adımın bu hata
koduyla reddedilmesi beklenir), `label` ve `continue_on_failure` eklenebilir.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..engine import behaviours  # noqa: F401  (BEHAVIOURS registry'i doldurur)
from ..engine.executor import BEHAVIOURS
from ..oracle.assertions import normalize_assert_args

STEP_META_KEYS = {"expect", "label", "continue_on_failure", "expect_error", "agent"}

# setup işlemi -> sunucuya gönderilecek `/qa` komutu
SETUP_OPS: dict[str, str] = {
    "reset": "Karakteri başlangıç durumuna döndür",
    "give_item": "{vnum, count}",
    "set_gold": "yang miktarı",
    "set_level": "seviye",
    "set_hp": "hp",
    "teleport": "{map, x, y}",
    "clear_inventory": "",
}


def _split(raw: Any, kind: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Ham YAML öğesini (ad, argümanlar, meta) olarak ayır."""
    if isinstance(raw, str):
        return raw, {}, {}
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"Geçersiz {kind}: {raw!r}")
    meta = {k: v for k, v in raw.items() if k in STEP_META_KEYS}
    keys = [k for k in raw if k not in STEP_META_KEYS]
    if len(keys) != 1:
        raise ValueError(f"Her {kind} tek bir anahtar içermeli, bulunan: {keys}")
    name = keys[0]
    val = raw[name]
    if val is None:
        args: dict[str, Any] = {}
    elif isinstance(val, dict):
        args = dict(val)
    else:
        args = {"value": val}
    return name, args, meta


def _only_agent_meta(meta: dict[str, Any], kind: str) -> str | None:
    extra = set(meta) - {"agent"}
    if extra:
        raise ValueError(f"{kind} içinde {sorted(extra)} kullanılamaz")
    return meta.get("agent")


def _with_agent(base: Any, agent: str | None) -> Any:
    if agent is None:
        return base
    return {**(base if isinstance(base, dict) else {base: None}), "agent": agent}


class AssertSpec(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    # Çoklu ajanda hangi ajanın durumuna bakılacağı (verilmezse ilk ajan)
    agent: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> "AssertSpec":
        name, args, meta = _split(raw, "assert")
        agent = _only_agent_meta(meta, "assert")
        return cls(name=name, args=normalize_assert_args(name, args), agent=agent)

    def to_yaml(self) -> Any:
        return _with_agent({self.name: self.args} if self.args else self.name, self.agent)


class Step(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None
    expect: list[AssertSpec] = Field(default_factory=list)
    # Adımın bu hata koduyla reddedilmesi bekleniyor (ör. NOT_ENOUGH_GOLD) — edge-case testleri için
    expect_error: str | None = None
    continue_on_failure: bool = False
    # Çoklu ajanda adımı hangi ajan yapar (verilmezse ilk ajan)
    agent: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> "Step":
        name, args, meta = _split(raw, "adım")
        b = BEHAVIOURS.get(name)
        if b is None:
            raise ValueError(f"Bilinmeyen adım: {name}. Mevcut: {sorted(BEHAVIOURS)}")
        try:
            args = b.normalize_args(args)
        except TypeError as e:
            raise ValueError(f"{name}: {e}") from e
        return cls(name=name, args=args, label=meta.get("label"),
                   expect=[AssertSpec.parse(a) for a in meta.get("expect") or []],
                   expect_error=meta.get("expect_error"), agent=meta.get("agent"),
                   continue_on_failure=bool(meta.get("continue_on_failure", False)))

    def to_yaml(self) -> Any:
        d: dict[str, Any] = {self.name: self.args or None}
        if self.label:
            d["label"] = self.label
        if self.expect:
            d["expect"] = [a.to_yaml() for a in self.expect]
        if self.expect_error:
            d["expect_error"] = self.expect_error
        if self.continue_on_failure:
            d["continue_on_failure"] = True
        if self.agent:
            d["agent"] = self.agent
        return d if len(d) > 1 or self.args else self.name


class SetupOp(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> "SetupOp":
        name, args, meta = _split(raw, "setup")
        if name not in SETUP_OPS:
            raise ValueError(f"Bilinmeyen setup işlemi: {name}. Mevcut: {sorted(SETUP_OPS)}")
        return cls(name=name, args=args, agent=_only_agent_meta(meta, "setup"))

    def to_command(self) -> str:
        a = self.args
        v = a.get("value")
        if self.name == "reset":
            return "/qa reset"
        if self.name == "give_item":
            return f"/qa item {int(a.get('vnum', v))} {int(a.get('count', 1))}"
        if self.name == "set_gold":
            return f"/qa gold {int(a.get('gold', v))}"
        if self.name == "set_level":
            return f"/qa level {int(a.get('level', v))}"
        if self.name == "set_hp":
            return f"/qa hp {int(a.get('hp', v))}"
        if self.name == "teleport":
            return f"/qa warp {int(a['map'])} {int(a['x'])} {int(a['y'])}"
        if self.name == "clear_inventory":
            return "/qa clear_inventory"
        raise ValueError(self.name)

    def to_yaml(self) -> Any:
        if not self.args:
            return _with_agent(self.name, self.agent)
        return _with_agent({self.name: self.args["value"] if list(self.args) == ["value"] else self.args},
                           self.agent)


class OracleOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Varsayılan olarak her run'da SYSERR/log hatası ve QA_ASSERT ihlali olmaması beklenir
    allow_server_errors: bool = False
    allow_qa_asserts: bool = False


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str
    character: str | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    # Bu senaryonun test ettiği kaynak dosyalar (glob). Değişikliğe göre test seçiminde kullanılır.
    covers: list[str] = Field(default_factory=list)
    account: str | None = None
    character: str | None = None
    # Çoklu ajan: ad -> hesap. İlk ajan "birincil"dir (agent verilmeyen adımlar onundur).
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    seed: int | None = None
    reset: bool = True
    timeout_ms: int = 600000
    # Yalnızca simülatörde: bilinçli hata enjeksiyonu (oracle'ı doğrulamak için)
    sim_faults: list[str] = Field(default_factory=list)
    oracle: OracleOptions = Field(default_factory=OracleOptions)
    setup: list[SetupOp] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    asserts: list[AssertSpec] = Field(default_factory=list, alias="assert")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", v):
            raise ValueError("name yalnızca harf, rakam, _ ve - içerebilir")
        return v

    @model_validator(mode="after")
    def _check_agents(self) -> "Scenario":
        import re

        if self.agents and (self.account or self.character):
            raise ValueError("agents kullanılırken account/character yerine agents içindeki hesaplar kullanılır")
        for n in self.agents:
            if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", n):
                raise ValueError(f"Geçersiz ajan adı: {n!r}")
        accounts = [a.account for a in self.agents.values()]
        if len(set(accounts)) != len(accounts):
            raise ValueError("Her ajanın hesabı farklı olmalı")
        refs = [("setup", x.agent) for x in self.setup] + [("steps", x.agent) for x in self.steps]
        refs += [("assert", x.agent) for x in self.asserts]
        refs += [("expect", e.agent) for st in self.steps for e in st.expect]
        for where, a in refs:
            if a is not None and a not in self.agents:
                raise ValueError(f"{where}: bilinmeyen ajan '{a}' (tanımlı: {sorted(self.agents) or 'yok'})")
        return self

    @property
    def agent_count(self) -> int:
        return max(1, len(self.agents))

    def agent_specs(self) -> dict[str, AgentSpec]:
        """Tek ajanlı senaryolar için de tek elemanlı ajan listesi döndür."""
        if self.agents:
            return dict(self.agents)
        return {"": AgentSpec.model_construct(account=self.account, character=self.character)}

    @model_validator(mode="before")
    @classmethod
    def _parse_lists(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        data["setup"] = [x if isinstance(x, SetupOp) else SetupOp.parse(x) for x in data.get("setup") or []]
        data["steps"] = [x if isinstance(x, Step) else Step.parse(x) for x in data.get("steps") or []]
        key = "assert" if "assert" in data else "asserts"
        data["assert"] = [x if isinstance(x, AssertSpec) else AssertSpec.parse(x) for x in data.pop(key, None) or []]
        return data

    def to_yaml_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.description:
            d["description"] = self.description
        for k in ("tags", "covers", "account", "character", "seed", "sim_faults"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.agents:
            d["agents"] = {k: v.model_dump(exclude_none=True) for k, v in self.agents.items()}
        if not self.reset:
            d["reset"] = False
        if self.oracle != OracleOptions():
            d["oracle"] = self.oracle.model_dump()
        if self.setup:
            d["setup"] = [s.to_yaml() for s in self.setup]
        d["steps"] = [s.to_yaml() for s in self.steps]
        if self.asserts:
            d["assert"] = [a.to_yaml() for a in self.asserts]
        return d
