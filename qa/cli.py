"""Komut satırı: CI ve manuel kullanım için.

    metin2-qa list
    metin2-qa run kill_mob_pickup --seed 846219
    metin2-qa run --all [--tag smoke]       # hata varsa çıkış kodu 1
    metin2-qa replay QA-2026-00012 --times 5
    metin2-qa runs --status FAILED
    metin2-qa show QA-2026-00012 [--trace]
    metin2-qa reference
    metin2-qa sim-server --port 47800 --fault no_drop
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_config


def _print(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="metin2-qa", description="Metin2 AI QA Player")
    ap.add_argument("--config", help="qa.toml yolu (varsayılan: QA_CONFIG / ./qa.local.toml / ./qa.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Senaryoları listele")
    p = sub.add_parser("run", help="Senaryo çalıştır")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--tag")
    p.add_argument("--seed", type=int)
    p = sub.add_parser("replay", help="Run'ı aynı seed ile tekrar oynat")
    p.add_argument("run_id")
    p.add_argument("--times", type=int, default=3)
    p = sub.add_parser("runs", help="Son run'lar")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("show", help="Run raporu")
    p.add_argument("run_id")
    p.add_argument("--trace", action="store_true")
    sub.add_parser("reference", help="Behaviour/assertion referansı")
    p = sub.add_parser("sim-server", help="Simülatörü TCP'de sun")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("mcp", help="MCP sunucusunu stdio'da başlat")

    a = ap.parse_args(argv)

    if a.cmd == "sim-server":
        from .sim.server import main as sim_main

        sim_main(a.rest)
        return 0
    if a.cmd == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return 0

    from .service import QaService

    s = QaService(load_config(a.config))
    if a.cmd == "list":
        for sc in s.list_scenarios():
            print(f"{sc['name']:28} {','.join(sc.get('tags', [])):28} {sc.get('description') or sc.get('error', '')}")
        return 0
    if a.cmd == "run":
        if a.all or a.tag:
            res = s.run_suite(a.tag, a.seed)
            for r in res["results"]:
                print(r["summary"])
            print(json.dumps(res["counts"]))
            return 0 if res["ok"] else 1
        if not a.names:
            ap.error("senaryo adı veya --all gerekli")
        ok = True
        for n in a.names:
            rep = s.run_scenario(n, a.seed)
            print(rep["summary"])
            print(f"  kanıt: {s.cfg.artifacts_path / rep['run_id']}")
            ok &= rep["result"] == "PASSED"
        return 0 if ok else 1
    if a.cmd == "replay":
        res = s.replay_failure(a.run_id, a.times)
        print(f"{res['verdict']} (deterministic_trace={res['deterministic_trace']})")
        for r in res["runs"]:
            print(f"  {r['summary']}")
        return 0
    if a.cmd == "runs":
        for r in s.get_test_runs(a.limit, a.status):
            print(r["summary"] or f"{r['run_id']} {r['scenario']} {r['status']}")
        return 0
    if a.cmd == "show":
        if a.trace:
            print(s.get_trace(a.run_id, 0))
        else:
            _print(s.get_test_result(a.run_id))
        return 0
    if a.cmd == "reference":
        _print(s.reference())
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
