"""QA Oracle assertion'ları: "Bir şey yanlış mı?"

Her assertion gözlemi (anlık oyun durumu + başlangıç anlık görüntüsü + sunucu sinyalleri)
okur ve beklenen/gerçekleşen değerleri içeren bir AssertionResult döndürür. Karşılaştırma
anahtarları: equals, min, max, delta (başlangıca göre fark).
"""

from __future__ import annotations

import inspect
import math
from dataclasses import asdict, dataclass
from typing import Any, Callable

from ..engine.executor import GameContext
from .signals import ServerSignals


@dataclass
class AssertionResult:
    name: str
    params: dict[str, Any]
    passed: bool
    expected: Any
    actual: Any
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Observation:
    """Assertion'ların gördüğü dünya. Durum sorguları önbelleklenir."""

    def __init__(self, ctx: GameContext, baseline: dict[str, Any], signals: ServerSignals):
        self.ctx = ctx
        self.baseline = baseline
        self.signals = signals
        self._cache: dict[str, Any] = {}

    def _get(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    @property
    def state(self) -> dict[str, Any]:
        return self._get("state", self.ctx.state)

    @property
    def inventory(self) -> dict[str, Any]:
        return self._get("inventory", self.ctx.inventory)

    @property
    def quests(self) -> dict[str, Any]:
        return self._get("quests", lambda: self.ctx.query("get_quest_state"))

    @property
    def windows(self) -> dict[str, Any]:
        return self._get("windows", self.ctx.windows)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._get("messages", lambda: self.ctx.query("get_system_messages", since=0))

    def item_count(self, inv: dict[str, Any], vnum: int) -> int:
        return sum(i["count"] for i in inv["items"] if i["vnum"] == vnum)


def take_snapshot(ctx: GameContext) -> dict[str, Any]:
    return {"state": ctx.state(), "inventory": ctx.inventory(), "quests": ctx.query("get_quest_state"),
            "event_index": len(ctx.events)}


ASSERTIONS: dict[str, Callable[..., AssertionResult]] = {}


def assertion(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        ASSERTIONS[name] = fn
        return fn

    return deco


def _compare(name: str, params: dict[str, Any], actual: Any, baseline: Any = None,
             equals: Any = None, min: Any = None, max: Any = None, delta: Any = None) -> AssertionResult:
    expected: dict[str, Any] = {}
    passed = True
    if equals is not None:
        expected["equals"] = equals
        passed &= actual == equals
    if min is not None:
        expected["min"] = min
        passed &= actual is not None and actual >= min
    if max is not None:
        expected["max"] = max
        passed &= actual is not None and actual <= max
    actual_out: Any = actual
    if delta is not None:
        d = None if actual is None or baseline is None else actual - baseline
        expected["delta"] = delta
        actual_out = {"value": actual, "baseline": baseline, "delta": d}
        passed &= d == delta
    if not expected:
        raise ValueError(f"{name}: equals/min/max/delta'dan en az biri gerekli")
    return AssertionResult(name, params, bool(passed), expected, actual_out)


# ---------------------------------------------------------------------- envanter

@assertion("inventory_contains")
def inventory_contains(obs: Observation, vnum: int, count: int = 1, exact: bool = False) -> AssertionResult:
    n = obs.item_count(obs.inventory, vnum)
    ok = n == count if exact else n >= count
    exp = {"vnum": vnum, ("count" if exact else "min_count"): count}
    return AssertionResult("inventory_contains", {"vnum": vnum, "count": count, "exact": exact}, ok, exp,
                           {"vnum": vnum, "count": n})


@assertion("inventory_not_contains")
def inventory_not_contains(obs: Observation, vnum: int) -> AssertionResult:
    n = obs.item_count(obs.inventory, vnum)
    return AssertionResult("inventory_not_contains", {"vnum": vnum}, n == 0, {"vnum": vnum, "count": 0},
                           {"vnum": vnum, "count": n})


@assertion("item_count")
def item_count(obs: Observation, vnum: int, equals: int | None = None, min: int | None = None,
               max: int | None = None, delta: int | None = None) -> AssertionResult:
    p = {"vnum": vnum, "equals": equals, "min": min, "max": max, "delta": delta}
    return _compare("item_count", p, obs.item_count(obs.inventory, vnum),
                    obs.item_count(obs.baseline["inventory"], vnum), equals, min, max, delta)


@assertion("equipped")
def equipped(obs: Observation, vnum: int | None = None, wear: str | None = None) -> AssertionResult:
    eq = obs.inventory.get("equipment", {})
    if wear is not None:
        it = eq.get(wear)
        ok = it is not None and (vnum is None or it["vnum"] == vnum)
        actual = {"wear": wear, "vnum": it["vnum"] if it else None}
    else:
        ok = any(v["vnum"] == vnum for v in eq.values())
        actual = {k: v["vnum"] for k, v in eq.items()}
    return AssertionResult("equipped", {"vnum": vnum, "wear": wear}, ok, {"vnum": vnum, "wear": wear}, actual)


# ---------------------------------------------------------------------- oyuncu

def _player_field(obs: Observation, field: str, **cmp: Any) -> AssertionResult:
    if field not in obs.state:
        return AssertionResult(field, cmp, False, cmp, None, f"Oyuncu durumunda '{field}' alanı yok")
    return _compare(field, {k: v for k, v in cmp.items() if v is not None}, obs.state.get(field),
                    obs.baseline["state"].get(field), **cmp)


@assertion("gold")
def gold(obs: Observation, equals: int | None = None, min: int | None = None, max: int | None = None,
         delta: int | None = None) -> AssertionResult:
    return _player_field(obs, "gold", equals=equals, min=min, max=max, delta=delta)


@assertion("level")
def level(obs: Observation, equals: int | None = None, min: int | None = None, max: int | None = None,
          delta: int | None = None) -> AssertionResult:
    return _player_field(obs, "level", equals=equals, min=min, max=max, delta=delta)


@assertion("hp")
def hp(obs: Observation, equals: int | None = None, min: int | None = None, max: int | None = None,
       delta: int | None = None) -> AssertionResult:
    return _player_field(obs, "hp", equals=equals, min=min, max=max, delta=delta)


@assertion("player")
def player(obs: Observation, field: str, equals: Any = None, min: Any = None, max: Any = None,
           delta: Any = None) -> AssertionResult:
    r = _player_field(obs, field, equals=equals, min=min, max=max, delta=delta)
    r.name = f"player.{field}"
    return r


@assertion("alive")
def alive(obs: Observation) -> AssertionResult:
    dead = bool(obs.state.get("dead"))
    return AssertionResult("alive", {}, not dead, {"dead": False}, {"dead": dead, "hp": obs.state.get("hp")})


@assertion("position_near")
def position_near(obs: Observation, x: int, y: int, tolerance: int = 300) -> AssertionResult:
    s = obs.state
    d = math.hypot(s["x"] - x, s["y"] - y)
    return AssertionResult("position_near", {"x": x, "y": y, "tolerance": tolerance}, d <= tolerance,
                           {"x": x, "y": y, "max_distance": tolerance},
                           {"x": s["x"], "y": s["y"], "distance": round(d)})


# ---------------------------------------------------------------------- görev / UI

@assertion("quest")
def quest(obs: Observation, name: str, state: str | None = None, progress: int | None = None,
          min_progress: int | None = None) -> AssertionResult:
    q = obs.quests.get(name)
    expected = {k: v for k, v in {"state": state, "progress": progress, "min_progress": min_progress}.items()
                if v is not None}
    if q is None:
        return AssertionResult("quest", {"name": name, **expected}, False, expected, None, f"'{name}' görevi yok")
    ok = (state is None or q.get("state") == state) and (progress is None or q.get("progress") == progress) \
        and (min_progress is None or q.get("progress", 0) >= min_progress)
    return AssertionResult("quest", {"name": name, **expected}, ok, expected, q)


@assertion("window_open")
def window_open(obs: Observation, name: str) -> AssertionResult:
    return AssertionResult("window_open", {"name": name}, name in obs.windows, {"open": name}, sorted(obs.windows))


@assertion("window_closed")
def window_closed(obs: Observation, name: str) -> AssertionResult:
    return AssertionResult("window_closed", {"name": name}, name not in obs.windows, {"closed": name},
                           sorted(obs.windows))


@assertion("system_message")
def system_message(obs: Observation, contains: str) -> AssertionResult:
    texts = [m["text"] for m in obs.messages]
    ok = any(contains.lower() in t.lower() for t in texts)
    return AssertionResult("system_message", {"contains": contains}, ok, {"contains": contains}, texts[-10:])


# ---------------------------------------------------------------------- olaylar / sunucu

@assertion("client_event")
def client_event(obs: Observation, name: str, count: int | None = None, min: int = 1,
                 match: dict[str, Any] | None = None) -> AssertionResult:
    evs = [e for e in obs.ctx.events[obs.baseline["event_index"]:] if e["event"] == name
           and all(e["data"].get(k) == v for k, v in (match or {}).items())]
    n = len(evs)
    ok = n == count if count is not None else n >= min
    exp = {"count": count} if count is not None else {"min": min}
    return AssertionResult("client_event", {"name": name, "match": match}, ok, exp, {"count": n})


@assertion("server_event")
def server_event(obs: Observation, name: str, count: int | None = None, min: int = 1,
                 match: dict[str, Any] | None = None) -> AssertionResult:
    obs.signals.poll()
    evs = [e for e in obs.signals.named(name)
           if all(e.get("data", {}).get(k) == v for k, v in (match or {}).items())]
    n = len(evs)
    ok = n == count if count is not None else n >= min
    exp = {"count": count} if count is not None else {"min": min}
    return AssertionResult("server_event", {"name": name, "match": match}, ok, exp, {"count": n})


@assertion("server_errors")
def server_errors(obs: Observation, equals: int | None = None, max: int | None = None) -> AssertionResult:
    if equals is None and max is None:
        equals = 0
    obs.signals.poll()
    errs = obs.signals.errors
    n = len(errs)
    ok = (equals is None or n == equals) and (max is None or n <= max)
    return AssertionResult("server_errors", {"equals": equals, "max": max}, ok,
                           {k: v for k, v in {"equals": equals, "max": max}.items() if v is not None},
                           {"count": n, "errors": errs[:10]})


@assertion("qa_asserts")
def qa_asserts(obs: Observation, equals: int = 0) -> AssertionResult:
    obs.signals.poll()
    fails = obs.signals.assert_failures
    return AssertionResult("qa_asserts", {"equals": equals}, len(fails) == equals, {"equals": equals},
                           {"count": len(fails), "asserts": fails[:10]})


# ---------------------------------------------------------------------- yardımcılar

def describe_assertions() -> list[dict[str, Any]]:
    out = []
    for name, fn in ASSERTIONS.items():
        ps = list(inspect.signature(fn).parameters.values())[1:]
        out.append({"name": name, "params": {p.name: (None if p.default is inspect.Parameter.empty else p.default)
                                             for p in ps},
                    "required": [p.name for p in ps if p.default is inspect.Parameter.empty]})
    return out


def normalize_assert_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    fn = ASSERTIONS.get(name)
    if fn is None:
        raise ValueError(f"Bilinmeyen assertion: {name}. Mevcut: {sorted(ASSERTIONS)}")
    args = dict(args)
    ps = list(inspect.signature(fn).parameters.values())[1:]
    if "value" in args and "value" not in {p.name for p in ps}:
        args[ps[0].name] = args.pop("value")
    try:
        inspect.signature(fn).bind(None, **args)
    except TypeError as e:
        raise ValueError(f"{name}: {e}") from e
    return args


def evaluate(obs: Observation, name: str, args: dict[str, Any]) -> AssertionResult:
    try:
        return ASSERTIONS[name](obs, **normalize_assert_args(name, args))
    except ValueError as e:
        return AssertionResult(name, args, False, None, None, str(e))
