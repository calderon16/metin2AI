"""SQLite rapor deposu: run'lar ve hatalar."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    scenario TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    seed INTEGER,
    account TEXT,
    git_commit TEXT,
    branch TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    game_time_ms INTEGER,
    replay_of TEXT,
    summary TEXT,
    trace_digest TEXT,
    artifacts_dir TEXT
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS runs_scenario ON runs(scenario);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    step INTEGER,
    action TEXT,
    kind TEXT NOT NULL,
    name TEXT,
    expected TEXT,
    actual TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS failures_run ON failures(run_id);
"""

STATUSES = {"RUNNING", "PASSED", "FAILED", "ERROR"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Daemon'da birden çok thread aynı bağlantıyı kullanır: tüm erişim tek kilitten geçer
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    # -- genel yardımcılar (daemon tabloları bunları kullanır)
    def executescript(self, script: str) -> None:
        with self._lock:
            self._db.executescript(script)

    def execute(self, sql: str, args: tuple | list = ()) -> int:
        with self._lock, self._db:
            return self._db.execute(sql, args).lastrowid

    def query(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def query_one(self, sql: str, args: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def close(self) -> None:
        self._db.close()

    def new_run(self, scenario: str, mode: str, seed: int | None, account: str | None,
                git_commit: str | None, branch: str | None, artifacts_root: Path,
                replay_of: str | None = None) -> tuple[str, Path]:
        with self._lock, self._db:
            seq = (self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM runs").fetchone()[0]) + 1
            run_id = f"QA-{datetime.now().year}-{seq:05d}"
            adir = artifacts_root / run_id
            self._db.execute(
                "INSERT INTO runs (run_id, seq, scenario, mode, status, seed, account, git_commit, branch,"
                " started_at, replay_of, artifacts_dir) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, seq, scenario, mode, "RUNNING", seed, account, git_commit, branch, utcnow(),
                 replay_of, str(adir)),
            )
        adir.mkdir(parents=True, exist_ok=True)
        return run_id, adir

    def finish_run(self, run_id: str, status: str, summary: str, game_time_ms: int | None,
                   trace_digest: str | None, failures: list[dict[str, Any]]) -> None:
        assert status in STATUSES
        with self._lock, self._db:
            self._db.execute(
                "UPDATE runs SET status=?, summary=?, finished_at=?, game_time_ms=?, trace_digest=? WHERE run_id=?",
                (status, summary, utcnow(), game_time_ms, trace_digest, run_id),
            )
            self._db.execute("DELETE FROM failures WHERE run_id=?", (run_id,))
            for f in failures:
                self._db.execute(
                    "INSERT INTO failures (run_id, step, action, kind, name, expected, actual, message)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, f.get("step"), f.get("action"), f["kind"], f.get("name"),
                     json.dumps(f.get("expected"), ensure_ascii=False),
                     json.dumps(f.get("actual"), ensure_ascii=False), f.get("message")),
                )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM runs WHERE run_id=?", (run_id,))

    def list_runs(self, limit: int = 20, status: str | None = None, scenario: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM runs WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status.upper())
        if scenario:
            q += " AND scenario=?"
            args.append(scenario)
        q += " ORDER BY seq DESC LIMIT ?"
        args.append(int(limit))
        return self.query(q, args)

    def failures(self, run_id: str) -> list[dict[str, Any]]:
        out = []
        for d in self.query("SELECT * FROM failures WHERE run_id=? ORDER BY id", (run_id,)):
            for k in ("expected", "actual"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return out
