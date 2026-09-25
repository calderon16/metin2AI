"""Daemon tabloları: işler, bulgular, kampanyalar, zamanlayıcı durumu.

Run tablolarıyla aynı SQLite dosyasını ve `Store`'un kilitli bağlantısını kullanır.
"""

from __future__ import annotations

import json
from typing import Any

from ..store.db import Store, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    params TEXT NOT NULL,
    status TEXT NOT NULL,            -- queued | running | done | failed | cancelled | interrupted
    priority INTEGER NOT NULL DEFAULT 0,
    source TEXT,                     -- panel | schedule:<ad> | mcp | api | cli
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    agents TEXT,
    run_ids TEXT,
    progress TEXT,
    result TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT NOT NULL UNIQUE,
    scenario TEXT,
    system TEXT,
    title TEXT NOT NULL,
    kind TEXT,
    severity TEXT,
    status TEXT NOT NULL,            -- new | confirmed | flaky | not_reproduced | fixed | regressed | ignored
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    last_run_id TEXT,
    run_ids TEXT,
    details TEXT,
    confirm TEXT,
    fixed_at TEXT,
    fixed_by_run TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS findings_status ON findings(status);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    matrix TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS schedule_state (
    name TEXT PRIMARY KEY,
    last_run TEXT,
    last_job_id INTEGER
);
"""

JSON_COLS = {"params", "agents", "run_ids", "progress", "result", "details", "confirm", "matrix", "summary"}


def _dec(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    for k in JSON_COLS & set(row):
        if row[k] is not None:
            try:
                row[k] = json.loads(row[k])
            except ValueError:
                pass
    return row


def _enc(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


class DaemonDB:
    def __init__(self, store: Store):
        self.store = store
        store.executescript(SCHEMA)

    # ------------------------------------------------------------------ jobs
    def create_job(self, type: str, params: dict[str, Any], source: str = "api", priority: int = 0) -> int:
        return self.store.execute(
            "INSERT INTO jobs (type, params, status, priority, source, created_at) VALUES (?,?,?,?,?,?)",
            (type, _enc(params), "queued", priority, source, utcnow()))

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [_enc(v) if k in JSON_COLS else v for k, v in fields.items()]
        self.store.execute(f"UPDATE jobs SET {cols} WHERE job_id=?", (*vals, job_id))

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return _dec(self.store.query_one("SELECT * FROM jobs WHERE job_id=?", (job_id,)))

    def list_jobs(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q, a = "SELECT * FROM jobs", []
        if status:
            q += " WHERE status IN (%s)" % ",".join("?" * len(status.split(",")))
            a += status.split(",")
        q += " ORDER BY job_id DESC LIMIT ?"
        return [_dec(r) for r in self.store.query(q, (*a, int(limit)))]

    def next_queued(self) -> dict[str, Any] | None:
        return _dec(self.store.query_one(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY priority DESC, job_id ASC LIMIT 1"))

    def interrupt_running(self) -> int:
        rows = self.store.query("SELECT job_id FROM jobs WHERE status='running'")
        for r in rows:
            self.update_job(r["job_id"], status="interrupted", finished_at=utcnow(),
                            error="Daemon yeniden başlatıldı")
        return len(rows)

    # ------------------------------------------------------------------ findings
    def get_finding(self, fid: int) -> dict[str, Any] | None:
        return _dec(self.store.query_one("SELECT * FROM findings WHERE id=?", (fid,)))

    def finding_by_signature(self, sig: str) -> dict[str, Any] | None:
        return _dec(self.store.query_one("SELECT * FROM findings WHERE signature=?", (sig,)))

    def insert_finding(self, **f: Any) -> int:
        cols = list(f)
        return self.store.execute(
            f"INSERT INTO findings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [_enc(f[k]) if k in JSON_COLS else f[k] for k in cols])

    def update_finding(self, fid: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [_enc(v) if k in JSON_COLS else v for k, v in fields.items()]
        self.store.execute(f"UPDATE findings SET {cols} WHERE id=?", (*vals, fid))

    def list_findings(self, status: str | None = None, scenario: str | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
        q, a = "SELECT * FROM findings WHERE 1=1", []
        if status:
            st = status.split(",")
            q += " AND status IN (%s)" % ",".join("?" * len(st))
            a += st
        if scenario:
            q += " AND scenario=?"
            a.append(scenario)
        q += " ORDER BY last_seen DESC, id DESC LIMIT ?"
        return [_dec(r) for r in self.store.query(q, (*a, int(limit)))]

    # ------------------------------------------------------------------ campaigns
    def create_campaign(self, job_id: int | None) -> int:
        return self.store.execute("INSERT INTO campaigns (job_id, status, started_at) VALUES (?,?,?)",
                                  (job_id, "running", utcnow()))

    def update_campaign(self, cid: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [_enc(v) if k in JSON_COLS else v for k, v in fields.items()]
        self.store.execute(f"UPDATE campaigns SET {cols} WHERE id=?", (*vals, cid))

    def get_campaign(self, cid: int) -> dict[str, Any] | None:
        return _dec(self.store.query_one("SELECT * FROM campaigns WHERE id=?", (cid,)))

    def list_campaigns(self, limit: int = 30) -> list[dict[str, Any]]:
        return [_dec(r) for r in self.store.query("SELECT * FROM campaigns ORDER BY id DESC LIMIT ?", (limit,))]

    def last_finished_campaign(self, before_id: int | None = None) -> dict[str, Any] | None:
        q, a = "SELECT * FROM campaigns WHERE status='done'", []
        if before_id is not None:
            q += " AND id<?"
            a.append(before_id)
        return _dec(self.store.query_one(q + " ORDER BY id DESC LIMIT 1", a))

    # ------------------------------------------------------------------ schedules
    def schedule_state(self, name: str) -> dict[str, Any] | None:
        return self.store.query_one("SELECT * FROM schedule_state WHERE name=?", (name,))

    def set_schedule_state(self, name: str, last_run: str, job_id: int) -> None:
        self.store.execute("INSERT INTO schedule_state (name, last_run, last_job_id) VALUES (?,?,?) "
                           "ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run, "
                           "last_job_id=excluded.last_job_id", (name, last_run, job_id))
