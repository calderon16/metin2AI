"""Zamanlayıcı: qa.toml [[daemon.schedules]] ile tekrarlayan işler.

    [[daemon.schedules]]
    name = "gece-kampanya"
    daily = "03:00"                 # her gün yerel saatle
    job = { type = "campaign" }

    [[daemon.schedules]]
    name = "saatlik-smoke"
    every = "1h"                    # s | m | h | d
    job = { type = "suite", tag = "smoke" }

    [[daemon.schedules]]
    name = "soak"
    continuous = true               # önceki bitince hemen yenisi: ajanlar sürekli oynar
    job = { type = "campaign", explore = true }
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .core import Daemon

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_every(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", str(text))
    if not m:
        raise ValueError(f"Geçersiz aralık: {text!r} (ör. 30m, 1h, 2d)")
    return float(m.group(1)) * _UNITS[m.group(2)]


def parse_daily(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"Geçersiz saat: {text!r} (ör. 03:00)")
    return int(m.group(1)), int(m.group(2))


def validate(schedules: list[dict[str, Any]], job_types: dict[str, str]) -> None:
    names = set()
    for s in schedules:
        n = s.get("name")
        if not n or n in names:
            raise ValueError(f"Zamanlama adı eksik ya da tekrar ediyor: {n!r}")
        names.add(n)
        modes = [k for k in ("every", "daily", "continuous") if s.get(k)]
        if len(modes) != 1:
            raise ValueError(f"{n}: every / daily / continuous'tan tam biri gerekli")
        if s.get("every"):
            parse_every(s["every"])
        if s.get("daily"):
            parse_daily(s["daily"])
        if (s.get("job") or {}).get("type") not in job_types:
            raise ValueError(f"{n}: job.type geçersiz ({sorted(job_types)})")


def is_due(s: dict[str, Any], last_run: datetime | None, now: datetime, active: bool) -> bool:
    """now: yerel saat (tz-aware). active: bu zamanlamanın işi hâlâ kuyrukta/çalışıyor mu."""
    if active or s.get("enabled", True) is False:
        return False
    if s.get("continuous"):
        return True
    if s.get("every"):
        return last_run is None or (now - last_run).total_seconds() >= parse_every(s["every"])
    h, m = parse_daily(s["daily"])
    # Bugünün (ya da henüz gelmediyse dünün) hedef zamanı
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < target:
        target -= timedelta(days=1)
    if last_run is None:
        # İlk çalıştırma: daemon hedef saatten sonraki 1 saat içinde açıldıysa kaçırma
        return now - target < timedelta(hours=1)
    return last_run < target


class Scheduler:
    def __init__(self, daemon: "Daemon"):
        self.daemon = daemon
        self.schedules = list(daemon.cfg.daemon.schedules)
        validate(self.schedules, _job_types())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.schedules:
            return
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def tick(self, now: datetime | None = None) -> list[int]:
        now = now or datetime.now().astimezone()
        created = []
        db = self.daemon.db
        for s in self.schedules:
            st = db.schedule_state(s["name"])
            last = datetime.fromisoformat(st["last_run"]) if st and st.get("last_run") else None
            active = False
            if st and st.get("last_job_id"):
                j = db.get_job(st["last_job_id"])
                active = bool(j and j["status"] in ("queued", "running"))
            if not is_due(s, last, now, active):
                continue
            job = dict(s["job"])
            jtype = job.pop("type")
            try:
                jid = self.daemon.jobs.submit(jtype, job, source=f"schedule:{s['name']}")
            except ValueError:
                continue  # ör. LLM anahtarı yoksa keşif zamanlaması atlanır
            db.set_schedule_state(s["name"], now.isoformat(), jid)
            created.append(jid)
        return created

    def status(self) -> list[dict[str, Any]]:
        out = []
        for s in self.schedules:
            st = self.daemon.db.schedule_state(s["name"]) or {}
            out.append({**s, "last_run": st.get("last_run"), "last_job_id": st.get("last_job_id")})
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(15)


def _job_types() -> dict[str, str]:
    from .jobs import JOB_TYPES

    return JOB_TYPES

