"""Oyun bağlamı (GameContext), action trace ve step çalıştırıcı."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..bridge.client import Bridge
from ..bridge.protocol import ActionError
from .rng import QaRandom


class BehaviourError(Exception):
    """Behaviour hedefine ulaşamadı (ör. mob bulunamadı, timeout, karakter öldü)."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


# Run'a özgü alanlar determinizm karşılaştırmasına girmez
_DIGEST_IGNORE = {"run_id"}


class Trace:
    """Yapılan her aksiyonun ve alınan her olayın sıralı kaydı (oyun zamanı ile)."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def add(self, kind: str, t: int, step: int | None, **fields: Any) -> dict[str, Any]:
        e = {"seq": len(self.entries) + 1, "t": t, "step": step, "kind": kind, **fields}
        self.entries.append(e)
        return e

    def write_jsonl(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as f:
            for e in self.entries:
                f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")

    def digest(self) -> str:
        """Replay'de determinizmi doğrulamak için trace özeti."""
        h = hashlib.sha256()
        for e in self.entries:
            e = {k: v for k, v in e.items() if k not in _DIGEST_IGNORE}
            h.update(json.dumps(e, ensure_ascii=False, sort_keys=True).encode())
        return h.hexdigest()[:16]

    def format_text(self) -> str:
        lines = []
        for e in self.entries:
            t = e["t"]
            ts = f"{t // 60000:02d}:{(t // 1000) % 60:02d}.{t % 1000:03d}"
            if e["kind"] == "action":
                args = " ".join(f"{k}={v}" for k, v in (e.get("args") or {}).items())
                status = "OK" if e.get("ok") else f"ERR {e.get('error')}"
                lines.append(f"{ts} [{e['step']}] {e['cmd']} {args} -> {status}")
            elif e["kind"] == "event":
                lines.append(f"{ts} [{e['step']}]   <- {e['event']} {e.get('data')}")
            else:
                rest = {k: v for k, v in e.items() if k not in {"seq", "t", "step", "kind"}}
                lines.append(f"{ts} [{e['step']}] {e['kind'].upper()} {rest}")
        return "\n".join(lines)


@dataclass
class GameContext:
    bridge: Bridge
    rng: QaRandom
    trace: Trace = field(default_factory=Trace)
    account: str | None = None
    password: str | None = None
    character: str | None = None
    screenshot_sink: Callable[[str, bytes, str], str] | None = None
    step: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ çekirdek
    def now(self) -> int:
        return self.bridge.last_t

    def _collect_events(self) -> None:
        for ev in self.bridge.drain_events():
            ev = {**ev, "step": self.step}
            self.events.append(ev)
            self.trace.add("event", ev.get("t", self.now()), self.step, event=ev["event"], data=ev.get("data"))

    def act(self, cmd: str, **args: Any) -> Any:
        """Oyuncu aksiyonu: trace'e yazılır, reddedilirse ActionError."""
        try:
            data = self.bridge.call(cmd, **args)
        except ActionError as e:
            self.trace.add("action", self.now(), self.step, cmd=cmd, args=_trace_args(cmd, args),
                           ok=False, error=f"{e.code}: {e.message}")
            self._collect_events()
            raise
        self.trace.add("action", self.now(), self.step, cmd=cmd, args=_trace_args(cmd, args), ok=True)
        self._collect_events()
        return data

    def query(self, cmd: str, **args: Any) -> Any:
        """Durum okuma: trace'e yazılmaz."""
        data = self.bridge.call(cmd, **args)
        self._collect_events()
        return data

    def note(self, kind: str, **fields: Any) -> None:
        self.trace.add(kind, self.now(), self.step, **fields)

    # ------------------------------------------------------------------ kısayollar
    def state(self) -> dict[str, Any]:
        return self.query("get_player_state")

    def inventory(self) -> dict[str, Any]:
        return self.query("get_inventory")

    def windows(self) -> dict[str, dict[str, Any]]:
        return {w["name"]: w for w in self.query("get_open_windows")}

    def entities(self, **filters: Any) -> list[dict[str, Any]]:
        return self.query("get_nearby_entities", **filters)

    def wait(self, ms: int) -> None:
        self.query("wait", ms=int(ms))

    def react(self) -> None:
        """İnsan benzeri reaksiyon gecikmesi (seed'e bağlı)."""
        self.wait(self.rng.reaction_delay())

    def item_count(self, vnum: int) -> int:
        return sum(i["count"] for i in self.inventory()["items"] if i["vnum"] == vnum)

    def find_slot(self, vnum: int) -> int | None:
        return next((i["slot"] for i in self.inventory()["items"] if i["vnum"] == vnum), None)

    def events_since(self, index: int, name: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.events[index:] if name is None or e["event"] == name]

    def screenshot(self, label: str) -> str | None:
        data = self.query("screenshot")
        if data.get("base64"):
            raw = base64.b64decode(data["base64"])
        elif data.get("path"):
            raw = Path(data["path"]).read_bytes()
        else:
            return None
        ext = str(data.get("format") or "png").lower().lstrip(".")
        path = self.screenshot_sink(label, raw, ext) if self.screenshot_sink else None
        self.note("screenshot", label=label, path=path)
        return path


def _trace_args(cmd: str, args: dict[str, Any]) -> dict[str, Any]:
    if cmd == "login":
        return {k: ("***" if k == "password" else v) for k, v in args.items()}
    return args


# ---------------------------------------------------------------------- registry

@dataclass
class Behaviour:
    name: str
    fn: Callable[..., Any]
    doc: str

    @property
    def params(self) -> list[inspect.Parameter]:
        return list(inspect.signature(self.fn).parameters.values())[1:]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "doc": self.doc,
            "params": {
                p.name: (None if p.default is inspect.Parameter.empty else p.default) for p in self.params
            },
            "required": [p.name for p in self.params if p.default is inspect.Parameter.empty],
        }

    def normalize_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """`wait: 1000` gibi skaler değerleri ilk parametreye bağlar ve imzayı doğrular."""
        args = dict(args)
        if "value" in args and "value" not in {p.name for p in self.params}:
            if not self.params:
                raise TypeError(f"'{self.name}' parametre almaz")
            args[self.params[0].name] = args.pop("value")
        inspect.signature(self.fn).bind(None, **args)
        return args


BEHAVIOURS: dict[str, Behaviour] = {}


def behaviour(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        BEHAVIOURS[name] = Behaviour(name, fn, inspect.cleandoc(fn.__doc__ or ""))
        return fn

    return deco


def execute_step(ctx: GameContext, name: str, args: dict[str, Any]) -> Any:
    from . import behaviours  # noqa: F401  (registry'i doldurur)

    b = BEHAVIOURS.get(name)
    if b is None:
        raise BehaviourError("UNKNOWN_BEHAVIOUR", f"Bilinmeyen adım: {name}", available=sorted(BEHAVIOURS))
    try:
        args = b.normalize_args(args)
    except TypeError as e:
        raise BehaviourError("BAD_ARGS", f"{name}: {e}") from e
    try:
        return b.fn(ctx, **args)
    except ActionError as e:
        raise BehaviourError("ACTION_REJECTED", f"{e.cmd} reddedildi: {e.code} {e.message}",
                             cmd=e.cmd, reject_code=e.code) from e
