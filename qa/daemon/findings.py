"""Bulgular: başarısız run'lardan ve keşiflerden çıkan hataların tek kaydı.

Aynı hata her gece tekrar görüldüğünde yeni kayıt açılmaz; imzasıyla tekilleştirilir, tekrar sayısı
artar. Yeni bulgu replay ile doğrulanır. Senaryo tekrar geçince "düzeldi" olur, düzelmiş bulgu yeniden
görülürse "geriledi" (regressed) olur. Panelden "yok say" ve not eklenebilir.
"""

from __future__ import annotations

import re
from typing import Any

from ..store.db import utcnow
from .db import DaemonDB

ACTIVE = {"new", "confirmed", "flaky", "not_reproduced", "regressed"}
STATUSES = ACTIVE | {"fixed", "ignored"}
SEVERITY_BY_KIND = {"oracle": "major", "assertion": "bug", "action_error": "bug", "timeout": "minor",
                    "finding": "bug"}


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zçğıöşü]+", "_", (text or "").lower()).strip("_")[:80]


def failure_key(scenario: str, f: dict[str, Any]) -> str:
    """Adım numarası senaryo düzenlenince kayabilir; imza aksiyon + hata adına dayanır."""
    return "|".join([scenario, f.get("agent") or "", f.get("kind") or "", str(f.get("name") or ""),
                     str(f.get("action") or "")])


class FindingStore:
    def __init__(self, db: DaemonDB):
        self.db = db

    def _upsert(self, sig: str, *, scenario: str | None, system: str | None, title: str, kind: str,
                severity: str, run_id: str, details: dict[str, Any]) -> tuple[int, str]:
        now = utcnow()
        cur = self.db.finding_by_signature(sig)
        if cur is None:
            fid = self.db.insert_finding(signature=sig, scenario=scenario, system=system, title=title, kind=kind,
                                         severity=severity, status="new", first_seen=now, last_seen=now,
                                         occurrences=1, last_run_id=run_id, run_ids=[run_id], details=details)
            return fid, "new"
        runs = (cur.get("run_ids") or [])[-19:] + [run_id]
        status = cur["status"]
        event = "seen"
        if status == "fixed":
            status, event = "regressed", "regressed"
        self.db.update_finding(cur["id"], last_seen=now, occurrences=cur["occurrences"] + 1, last_run_id=run_id,
                               run_ids=runs, details=details, status=status, system=system or cur.get("system"))
        return cur["id"], event

    def ingest(self, report: dict[str, Any], system: str | None = None) -> list[dict[str, Any]]:
        """Run raporunu işle. Döner: [{id, event: new|seen|regressed|fixed}]"""
        out: list[dict[str, Any]] = []
        scenario = report.get("scenario") or "?"
        run_id = report["run_id"]
        mode = report.get("mode")
        if report.get("result") == "PASSED" and mode in ("scenario", "replay"):
            for f in self.db.list_findings(",".join(ACTIVE), scenario=scenario):
                self.db.update_finding(f["id"], status="fixed", fixed_at=utcnow(), fixed_by_run=run_id)
                out.append({"id": f["id"], "event": "fixed"})
            return out
        if report.get("result") != "FAILED" or mode == "replay":
            return out
        saved = report.get("saved_scenario")
        for f in report.get("failures", []):
            kind = f.get("kind") or "assertion"
            if mode == "explore" and kind == "action_error":
                continue  # keşifte modelin hatalı çağrısı, oyun hatası değil
            if kind == "finding":
                sig = f"explore|{_norm(str(f.get('name')))}"
                title = str(f.get("name"))
            else:
                sig = failure_key(saved or scenario, f) if mode == "explore" else failure_key(scenario, f)
                who = f"{f['agent']}: " if f.get("agent") else ""
                act = f" ({who}{f['action']})" if f.get("action") else (f" ({who.rstrip(': ')})" if who else "")
                title = f"{saved or scenario}: {f.get('name')}{act}"
            sev = next((x.get("severity") for x in report.get("findings", [])
                        if x.get("title") == f.get("name")), None) or SEVERITY_BY_KIND.get(kind, "bug")
            details = {"message": f.get("message"), "expected": f.get("expected"), "actual": f.get("actual"),
                       "step": f.get("step"), "agent": f.get("agent"), "summary": report.get("summary")}
            fid, event = self._upsert(sig, scenario=saved or (scenario if mode != "explore" else None),
                                      system=system, title=title, kind=kind, severity=sev, run_id=run_id,
                                      details=details)
            out.append({"id": fid, "event": event})
        return out

    def record_confirmation(self, fid: int, replay: dict[str, Any]) -> str:
        k, n = replay["reproduced"], replay["total"]
        status = "confirmed" if k == n else ("flaky" if k else "not_reproduced")
        cur = self.db.get_finding(fid)
        if cur and cur["status"] in ("new", "regressed", "confirmed", "flaky", "not_reproduced"):
            self.db.update_finding(fid, status=status,
                                   confirm={"verdict": replay["verdict"], "at": utcnow(),
                                            "runs": [r["run_id"] for r in replay["runs"]]})
        return status

    def set_status(self, fid: int, status: str | None = None, note: str | None = None) -> dict[str, Any]:
        cur = self.db.get_finding(fid)
        if cur is None:
            raise KeyError(f"Bulgu yok: {fid}")
        fields: dict[str, Any] = {}
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"Geçersiz durum: {status} ({sorted(STATUSES)})")
            fields["status"] = status
        if note is not None:
            fields["note"] = note
        if fields:
            self.db.update_finding(fid, **fields)
        return self.db.get_finding(fid)

    def counts(self) -> dict[str, int]:
        rows = self.db.store.query("SELECT status, COUNT(*) AS n FROM findings GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
