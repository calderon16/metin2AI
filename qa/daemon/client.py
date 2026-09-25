"""Daemon HTTP istemcisi — MCP sunucusu ve CLI'nin 7/24 servise iş göndermesi için.

    QA_DAEMON_URL=http://127.0.0.1:8765   QA_PANEL_TOKEN=...   (şifre tanımlıysa)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlencode


class DaemonError(Exception):
    pass


class DaemonClient:
    def __init__(self, url: str, token: str | None = None, timeout_s: float = 30.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls) -> "DaemonClient | None":
        url = os.environ.get("QA_DAEMON_URL")
        return cls(url, os.environ.get("QA_PANEL_TOKEN")) if url else None

    def _req(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        q = {k: v for k, v in (query or {}).items() if v is not None}
        url = f"{self.url}/api{path}" + (f"?{urlencode(q)}" if q else "")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error")
            except ValueError:
                msg = str(e)
            raise DaemonError(f"Daemon HTTP {e.code}: {msg}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise DaemonError(f"Daemon'a ulaşılamadı ({self.url}): {e}") from e

    # -- uç noktalar
    def status(self) -> dict[str, Any]:
        return self._req("GET", "/status")

    def agents(self) -> list[dict[str, Any]]:
        return self._req("GET", "/agents")

    def submit(self, job_type: str, params: dict[str, Any] | None = None, source: str = "mcp") -> dict[str, Any]:
        return self._req("POST", "/jobs", {"type": job_type, "params": params or {}, "source": source})

    def job(self, job_id: int) -> dict[str, Any]:
        return self._req("GET", f"/jobs/{int(job_id)}")

    def jobs(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._req("GET", "/jobs", query={"status": status, "limit": limit})

    def cancel(self, job_id: int) -> dict[str, Any]:
        return self._req("POST", f"/jobs/{int(job_id)}/cancel", {})

    def wait(self, job_id: int, timeout_s: float = 600.0, poll_s: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            j = self.job(job_id)
            if j["status"] in ("done", "failed", "cancelled", "interrupted"):
                return j
            if time.monotonic() > deadline:
                return {**j, "note": f"{timeout_s:.0f} sn içinde bitmedi; get_job ile takip edin"}
            time.sleep(poll_s)

    def runs(self, limit: int = 20, status: str | None = None, scenario: str | None = None) -> list[dict[str, Any]]:
        return self._req("GET", "/runs", query={"limit": limit, "status": status, "scenario": scenario})

    def run(self, run_id: str) -> dict[str, Any]:
        return self._req("GET", f"/runs/{quote(run_id)}")

    def trace(self, run_id: str, tail: int = 200) -> str:
        return self._req("GET", f"/runs/{quote(run_id)}/trace", query={"tail": tail})["text"]

    def logs(self, run_id: str, tail: int = 200) -> dict[str, str]:
        return self._req("GET", f"/runs/{quote(run_id)}/logs", query={"tail": tail})

    def findings(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._req("GET", "/findings", query={"status": status, "limit": limit})

    def update_finding(self, fid: int, status: str | None = None, note: str | None = None) -> dict[str, Any]:
        return self._req("POST", f"/findings/{int(fid)}", {"status": status, "note": note})

    def campaigns(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._req("GET", "/campaigns", query={"limit": limit})

    def campaign(self, cid: int) -> dict[str, Any]:
        return self._req("GET", f"/campaigns/{int(cid)}")
