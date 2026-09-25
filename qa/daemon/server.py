"""HTTP JSON API + web panel sunucusu (yalnızca standart kütüphane).

Güvenlik:
  * Varsayılan bind 127.0.0.1. Başka bir adrese bind etmek için panel şifresi (QA_PANEL_TOKEN ortam
    değişkeni) zorunludur.
  * Şifre tanımlıysa tüm /api istekleri `Authorization: Bearer <şifre>` (veya `X-QA-Token`) ister.
  * Artifact dosyaları yalnızca ilgili run klasörü içinden okunur (yol kaçışı engellenir).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from ..config import ConfigError
from ..scenario.loader import ScenarioError
from ..scenario.runner import load_run_report
from ..store.artifacts import read_artifact
from .core import Daemon
from .jobs import JOB_TYPES

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 1_000_000


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


Route = tuple[str, re.Pattern[str], Callable[..., Any]]


class Api:
    def __init__(self, daemon: Daemon):
        self.d = daemon
        self.routes: list[Route] = []
        r = self._route
        r("GET", r"/api/status", self.status)
        r("GET", r"/api/agents", lambda q, b: self.d.agents.list())
        r("POST", r"/api/agents", self.add_agent)
        r("POST", r"/api/agents/(?P<acc>[^/]+)/(?P<op>start|stop|reconnect)", self.agent_op)
        r("DELETE", r"/api/agents/(?P<acc>[^/]+)", self.remove_agent)
        r("GET", r"/api/job-types", lambda q, b: JOB_TYPES)
        r("GET", r"/api/jobs", self.list_jobs)
        r("POST", r"/api/jobs", self.create_job)
        r("GET", r"/api/jobs/(?P<jid>\d+)", self.get_job)
        r("POST", r"/api/jobs/(?P<jid>\d+)/cancel", lambda q, b, jid: self.d.jobs.cancel(int(jid)))
        r("GET", r"/api/runs", self.list_runs)
        r("GET", r"/api/runs/(?P<rid>[\w\-]+)", lambda q, b, rid: load_run_report(self.d.store, rid))
        r("GET", r"/api/runs/(?P<rid>[\w\-]+)/trace", self.run_trace)
        r("GET", r"/api/runs/(?P<rid>[\w\-]+)/logs", self.run_logs)
        r("GET", r"/api/runs/(?P<rid>[\w\-]+)/files/(?P<path>.+)", self.run_file)
        r("POST", r"/api/runs/(?P<rid>[\w\-]+)/replay", self.replay_run)
        r("GET", r"/api/findings", self.list_findings)
        r("GET", r"/api/findings/(?P<fid>\d+)", self.get_finding)
        r("POST", r"/api/findings/(?P<fid>\d+)", self.update_finding)
        r("POST", r"/api/findings/(?P<fid>\d+)/confirm", self.confirm_finding)
        r("GET", r"/api/campaigns", lambda q, b: self.d.db.list_campaigns(int(q.get("limit", 30))))
        r("GET", r"/api/campaigns/(?P<cid>\d+)", self.get_campaign)
        r("GET", r"/api/catalog", self.catalog)
        r("GET", r"/api/scenarios", lambda q, b: self.d.service.list_scenarios(q.get("tag")))
        r("GET", r"/api/scenarios/(?P<name>[\w\-]+)", self.get_scenario)
        r("GET", r"/api/schedules", lambda q, b: self.d.scheduler.status())
        r("GET", r"/api/llm-usage", lambda q, b: self.d.service.llm_usage(int(q.get("days", 30))))

    def _route(self, method: str, pattern: str, fn: Callable[..., Any]) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), fn))

    def dispatch(self, method: str, path: str, query: dict[str, str], body: Any) -> Any:
        allowed = False
        for m, rx, fn in self.routes:
            mt = rx.match(path)
            if mt:
                allowed = True
                if m == method:
                    return fn(query, body, **mt.groupdict())
        raise ApiError(405 if allowed else 404, "Yöntem desteklenmiyor" if allowed else "Bulunamadı")

    # ------------------------------------------------------------------ uç noktalar
    def status(self, q: dict[str, str], b: Any) -> Any:
        return self.d.status()

    def add_agent(self, q: dict[str, str], b: Any) -> Any:
        b = b or {}
        a = self.d.agents.add(b.get("account"), b.get("character"), bool(b.get("keep_online", True)),
                              b.get("tags") or [], start=bool(b.get("start", True)))
        return a.public()

    def agent_op(self, q: dict[str, str], b: Any, acc: str, op: str) -> Any:
        {"start": self.d.agents.enable, "stop": self.d.agents.disable,
         "reconnect": self.d.agents.reconnect}[op](unquote(acc))
        return self.d.agents.agents[unquote(acc)].public()

    def remove_agent(self, q: dict[str, str], b: Any, acc: str) -> Any:
        self.d.agents.remove(unquote(acc))
        return {"ok": True}

    def list_jobs(self, q: dict[str, str], b: Any) -> Any:
        return self.d.db.list_jobs(q.get("status"), int(q.get("limit", 50)))

    def create_job(self, q: dict[str, str], b: Any) -> Any:
        b = b or {}
        jid = self.d.jobs.submit(b.get("type", ""), b.get("params") or {}, source=b.get("source", "panel"),
                                 priority=int(b.get("priority", 0)))
        return self.d.db.get_job(jid)

    def get_job(self, q: dict[str, str], b: Any, jid: str) -> Any:
        j = self.d.db.get_job(int(jid))
        if j is None:
            raise ApiError(404, f"İş yok: {jid}")
        return j

    def list_runs(self, q: dict[str, str], b: Any) -> Any:
        return self.d.service.get_test_runs(int(q.get("limit", 50)), q.get("status"), q.get("scenario"))

    def run_trace(self, q: dict[str, str], b: Any, rid: str) -> Any:
        return {"text": self.d.service.get_trace(rid, int(q.get("tail", 0)))}

    def run_logs(self, q: dict[str, str], b: Any, rid: str) -> Any:
        tail = int(q.get("tail", 300))
        return {"server": self.d.service.get_server_logs(rid, tail), "client": self.d.service.get_client_logs(rid, tail)}

    def run_file(self, q: dict[str, str], b: Any, rid: str, path: str) -> Any:
        run = self.d.store.get_run(rid)
        if run is None:
            raise ApiError(404, "Run yok")
        try:
            p = read_artifact(Path(run["artifacts_dir"]), unquote(path))
        except (ValueError, FileNotFoundError) as e:
            raise ApiError(404, str(e)) from e
        return RawFile(p)

    def replay_run(self, q: dict[str, str], b: Any, rid: str) -> Any:
        jid = self.d.jobs.submit("replay", {"run_id": rid, "times": int((b or {}).get("times", 3))}, source="panel")
        return self.d.db.get_job(jid)

    def list_findings(self, q: dict[str, str], b: Any) -> Any:
        return self.d.db.list_findings(q.get("status"), q.get("scenario"), int(q.get("limit", 200)))

    def get_finding(self, q: dict[str, str], b: Any, fid: str) -> Any:
        f = self.d.db.get_finding(int(fid))
        if f is None:
            raise ApiError(404, "Bulgu yok")
        return f

    def update_finding(self, q: dict[str, str], b: Any, fid: str) -> Any:
        b = b or {}
        return self.d.findings.set_status(int(fid), b.get("status"), b.get("note"))

    def confirm_finding(self, q: dict[str, str], b: Any, fid: str) -> Any:
        jid = self.d.jobs.submit("confirm", {"finding_id": int(fid)}, source="panel")
        return self.d.db.get_job(jid)

    def get_campaign(self, q: dict[str, str], b: Any, cid: str) -> Any:
        c = self.d.db.get_campaign(int(cid))
        if c is None:
            raise ApiError(404, "Kampanya yok")
        return c

    def catalog(self, q: dict[str, str], b: Any) -> Any:
        from .campaign import scenarios_for

        scenarios = self.d.service.list_scenarios()
        last = (self.d.db.list_campaigns(1) or [None])[0]
        matrix = (last or {}).get("matrix") or {}
        return [{**s, "scenarios": scenarios_for(s, scenarios), "last_status": matrix.get(s["id"], {}).get("status")}
                for s in self.d.catalog()]

    def get_scenario(self, q: dict[str, str], b: Any, name: str) -> Any:
        return {"name": name, "yaml": self.d.service.get_scenario(name)}


class RawFile:
    def __init__(self, path: Path):
        self.path = path


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_handler(api: Api, token: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Metin2QA/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # sessiz
            pass

        def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _authorized(self) -> bool:
            if not token:
                return True
            got = self.headers.get("X-QA-Token") or ""
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                got = auth[7:]
            return hmac.compare_digest(got.encode(), token.encode())

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            p = (WEB_DIR / name).resolve()
            if WEB_DIR.resolve() not in p.parents or not p.is_file():
                p = WEB_DIR / "index.html"  # tek sayfalık uygulama
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send(200, p.read_bytes(), ctype)

        def _handle(self, method: str) -> None:
            u = urlparse(self.path)
            if not u.path.startswith("/api/"):
                if method in ("GET", "HEAD"):
                    return self._static(u.path)
                return self._json(404, {"error": "Bulunamadı"})
            if not self._authorized():
                return self._json(401, {"error": "Yetkisiz: panel şifresi gerekli"})
            query = {k: v[-1] for k, v in parse_qs(u.query).items()}
            body = None
            if method in ("POST", "DELETE"):
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._json(413, {"error": "İstek çok büyük"})
                raw = self.rfile.read(n) if n else b""
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        return self._json(400, {"error": "Geçersiz JSON"})
            try:
                out = api.dispatch(method, u.path, query, body)
            except ApiError as e:
                return self._json(e.status, {"error": e.message})
            except (KeyError, FileNotFoundError) as e:
                return self._json(404, {"error": str(e).strip("'\"")})
            except (ValueError, ScenarioError, ConfigError, TypeError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            if isinstance(out, RawFile):
                ctype = mimetypes.guess_type(out.path.name)[0] or "application/octet-stream"
                return self._send(200, out.path.read_bytes(), ctype)
            return self._json(200, out)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_HEAD(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    return Handler


class PanelServer:
    def __init__(self, daemon: Daemon, host: str | None = None, port: int | None = None):
        self.daemon = daemon
        dc = daemon.cfg.daemon
        self.host = host if host is not None else dc.host
        self.port = port if port is not None else dc.port
        self.token = os.environ.get(dc.token_env) or None
        if not _is_loopback(self.host) and not self.token:
            raise ConfigError(f"Panel {self.host} adresine açılacaksa {dc.token_env} ortam değişkeninde "
                              "panel şifresi tanımlanmalı")
        self.api = Api(daemon)
        self.httpd = ThreadingHTTPServer((self.host, self.port), make_handler(self.api, self.token))
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.httpd.server_address[1]}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="panel-http", daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
