"""Build / test sunucusu yönetimi — yalnızca qa.toml'da tanımlı komutlar çalıştırılır.

Claude'a serbest shell verilmez: `build("server")` sadece [build.commands].server listesini
çalıştırır. Komutlar shell'siz (argv listesi) çalışır; çıktı artifacts/builds/ altına yazılır.
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from typing import Any

from ..config import QaConfig

ERROR_LINE = re.compile(r"(error|hata|undefined reference|fatal|failed)", re.IGNORECASE)


class BuildManager:
    def __init__(self, cfg: QaConfig):
        self.cfg = cfg

    def _run(self, kind: str, name: str, cmd: list[str], cwd: str | None, timeout_s: int) -> dict[str, Any]:
        self.cfg.check_environment()
        if not cmd:
            return {"ok": False, "name": name, "error": f"{kind} komutu qa.toml'da tanımlı değil"}
        logs = self.cfg.artifacts_path / "builds"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / f"{datetime.now():%Y%m%d-%H%M%S}_{kind}_{name}.log"
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, cwd=self.cfg.resolve(cwd) if cwd else self.cfg.root, capture_output=True,
                               text=True, errors="replace", timeout=timeout_s)
            code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired as e:
            code = -1
            out = f"{e.stdout or ''}{e.stderr or ''}\n[TIMEOUT {timeout_s}s]"
            out = out if isinstance(out, str) else out.decode(errors="replace")
        except OSError as e:
            code, out = -1, f"Komut çalıştırılamadı: {e}"
        log_path.write_text(out, encoding="utf-8")
        lines = out.splitlines()
        return {
            "ok": code == 0,
            "name": name,
            "command": cmd,
            "exit_code": code,
            "duration_s": round(time.monotonic() - t0, 1),
            "log": str(log_path),
            "error_lines": [l for l in lines if ERROR_LINE.search(l)][:40],
            "output_tail": lines[-60:],
        }

    def targets(self) -> list[str]:
        return list(self.cfg.build.commands)

    def build(self, target: str | None = None) -> dict[str, Any]:
        cmds = self.cfg.build.commands
        if not cmds:
            return {"ok": False, "error": "Build komutu tanımlı değil ([build.commands] qa.toml)"}
        names = [target] if target else list(cmds)
        unknown = [n for n in names if n not in cmds]
        if unknown:
            return {"ok": False, "error": f"Bilinmeyen build hedefi: {unknown}", "targets": list(cmds)}
        results = []
        for n in names:
            r = self._run("build", n, cmds[n], self.cfg.build.cwd, self.cfg.build.timeout_s)
            results.append(r)
            if not r["ok"]:
                break
        return {"ok": all(r["ok"] for r in results), "results": results}

    def start_server(self) -> dict[str, Any]:
        return self._run("server", "start", self.cfg.server.start, self.cfg.server.cwd, 300)

    def stop_server(self) -> dict[str, Any]:
        return self._run("server", "stop", self.cfg.server.stop, self.cfg.server.cwd, 300)
