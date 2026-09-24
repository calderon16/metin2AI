"""Sunucu tarafı sinyaller: QA_EVENT / QA_ASSERT olayları, SYSERR ve log hataları.

Kaynaklar:
  * BridgeEventSource — simülatör `qa_server_events` komutu
  * FileEventSource   — integration/server/qa_event.cpp'nin yazdığı qa_events.jsonl
  * LogFileSource     — syserr/syslog gibi düz log dosyaları (run başından itibaren eklenen satırlar)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from ..bridge.client import Bridge


class EventSource(Protocol):
    def mark(self) -> None: ...
    def poll(self) -> list[dict[str, Any]]: ...


class BridgeEventSource:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.cursor = 0

    def mark(self) -> None:
        evs = self.bridge.call("qa_server_events", since=0)
        self.cursor = evs[-1]["seq"] if evs else 0

    def poll(self) -> list[dict[str, Any]]:
        evs = self.bridge.call("qa_server_events", since=self.cursor)
        if evs:
            self.cursor = evs[-1]["seq"]
        return evs


class _TailFile:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0

    def mark(self) -> None:
        self.offset = self.path.stat().st_size if self.path.exists() else 0

    def read_new(self) -> list[str]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:  # dosya döndürüldü/yeniden oluşturuldu
            self.offset = 0
        with self.path.open("rb") as f:
            f.seek(self.offset)
            data = f.read()
        # Yarım satırı bir sonraki okumaya bırak
        cut = data.rfind(b"\n") + 1
        self.offset += cut
        return data[:cut].decode("utf-8", errors="replace").splitlines()


class FileEventSource:
    def __init__(self, path: Path):
        self._tail = _TailFile(path)

    def mark(self) -> None:
        self._tail.mark()

    def poll(self) -> list[dict[str, Any]]:
        out = []
        for line in self._tail.read_new():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"type": "syserr", "name": "BAD_QA_EVENT_LINE", "data": {"line": line}})
        return out


class LogFileSource:
    def __init__(self, name: str, path: Path, patterns: list[str]):
        self.name = name
        self._tail = _TailFile(path)
        self._re = re.compile("|".join(re.escape(p) for p in patterns)) if patterns else None
        self.lines: list[str] = []

    def mark(self) -> None:
        self._tail.mark()

    def poll(self) -> list[dict[str, Any]]:
        out = []
        for line in self._tail.read_new():
            self.lines.append(line)
            if self._re and self._re.search(line):
                out.append({"type": "log_error", "name": self.name.upper(), "data": {"line": line}})
        return out


class ServerSignals:
    """Bir run boyunca sunucu sinyallerini toplar."""

    def __init__(self, sources: list[EventSource], character: str | None = None):
        self.sources = sources
        self.character = character
        self.events: list[dict[str, Any]] = []

    def mark(self) -> None:
        for s in self.sources:
            s.mark()

    def poll(self) -> list[dict[str, Any]]:
        new = []
        for s in self.sources:
            for ev in s.poll():
                # Başka karakterlere ait olayları ayıkla (oyuncusuz olaylar — ör. SYSERR — kalır)
                if self.character and ev.get("player") not in (None, self.character):
                    continue
                new.append(ev)
        self.events.extend(new)
        return new

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") in {"syserr", "log_error", "quest_error", "sql_error"}]

    @property
    def assert_failures(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == "assert_fail"]

    def named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("name") == name]

    def log_lines(self) -> dict[str, list[str]]:
        return {s.name: s.lines for s in self.sources if isinstance(s, LogFileSource)}

    def format_events(self) -> str:
        return "\n".join(
            f"[{e.get('t', '-')}] {e.get('type', '?').upper():12} {e.get('name', '')} "
            f"{e.get('player') or ''} {json.dumps(e.get('data', {}), ensure_ascii=False)}"
            for e in self.events
        )
