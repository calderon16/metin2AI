"""Komut satırı: CI ve manuel kullanım için.

    metin2-qa list
    metin2-qa run kill_mob_pickup --seed 846219
    metin2-qa run --all [--tag smoke]       # hata varsa çıkış kodu 1
    metin2-qa affected --base origin/main --run   # sadece değişikliğin etkilediği senaryolar
    metin2-qa explore "Dükkanı test et" --steps 40 --save-as auto_shop   # otonom keşif (GEMINI_API_KEY)
    metin2-qa explore --goals explore/goals.yaml
    metin2-qa replay QA-2026-00012 --times 5
    metin2-qa runs --status FAILED
    metin2-qa show QA-2026-00012 [--trace]
    metin2-qa reference
    metin2-qa sim-server --port 47800 --fault no_drop
    metin2-qa daemon                          # 7/24 servis + web panel (http://127.0.0.1:8765)
    metin2-qa campaign                        # her şeyi test et (tek seferlik)
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_config


def _print(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _write_summary(path: str | None, results: list[dict], title: str, selection: dict | None = None) -> None:
    if not path:
        return
    from .store.report import markdown_summary

    with open(path, "a", encoding="utf-8") as f:
        f.write(markdown_summary(results, title, selection) + "\n")


def _explore(s, a, ap) -> int:
    import yaml

    if a.goals:
        goals = yaml.safe_load(open(a.goals, encoding="utf-8")) or []
        if a.only:
            goals = [g for g in goals if g.get("name") == a.only]
    elif a.goal:
        goals = [{"name": "cli", "goal": a.goal, "save_as": a.save_as,
                  "setup": yaml.safe_load(a.setup) if a.setup else None}]
    else:
        ap.error("hedef metni veya --goals gerekli")
    serious = {"critical", "major", "bug"}
    rows, bad = [], False
    for g in goals:
        print(f"== {g.get('name')}: {g['goal'].strip()[:100]}")
        out = s.explore_auto(g["goal"], a.steps or g.get("steps"), g.get("save_as"), g.get("setup"), a.seed,
                             model=a.model)
        print(f"   {out['summary']}")
        print(f"   durma: {out['stop_reason']} · adım {out['steps']} · tur {out['turns']} · "
              f"token {out['usage'].get('total_tokens', 0)}")
        if out.get("agent_summary"):
            print(f"   ajan: {out['agent_summary']}")
        for f in out["findings"]:
            print(f"   [{f.get('severity')}] {f.get('title')}: {f.get('description')}")
            bad |= f.get("severity") in serious
        if out.get("saved_scenario"):
            print(f"   senaryo: {out['saved_scenario']} → doğrulama {(out.get('validation') or {}).get('result')}")
        rows.append({"scenario": f"explore:{g.get('name')}", "run_id": out["run_id"], "result": out["result"],
                     "summary": f"{len(out['findings'])} bulgu · {out['stop_reason']} · {out.get('agent_summary', '')}"})
    _write_summary(a.summary_md, rows, "Metin2 QA — otonom keşif")
    return 1 if a.fail_on_findings and bad else 0


def _packets(a) -> int:
    from pathlib import Path

    from .headless.bindings import Bindings
    from .headless.profile import Profile, import_profile

    if a.packets_cmd == "import":
        prof = import_profile([Path(x) for x in a.packet_h], [Path(x) for x in a.size_table],
                              [Path(x) for x in a.defines], a.define, a.name)
        prof.save(Path(a.output))
        print(f"Profil yazıldı: {a.output}")
        a = argparse.Namespace(profile=a.output)
    prof = Profile.load(Path(a.profile))
    b = Bindings.for_profile(prof, Path(a.profile))
    known = [h for h, v in prof.packets.items() if v.get("struct")]
    print(f"{prof.name}: {len(prof.structs)} struct, {len(prof.packets)} header ({len(known)} tanesinin struct'ı biliniyor)")
    missing = []
    for logical, spec in b["packets"].items():
        for d in ("cg", "gc"):
            cands = spec.get(d)
            if not cands:
                continue
            cands = cands if isinstance(cands, list) else [cands]
            hit = next((h for h in cands if h in prof.packets and prof.packets[h].get("struct")), None)
            print(f"  {'OK ' if hit else 'YOK'} {logical:16} {d.upper()} {hit or ' | '.join(cands)}")
            if not hit:
                missing.append(logical)
    if missing:
        print(f"Eksik bağlamalar: {sorted(set(missing))} — <profil>.bindings.yaml ile fork adlarını eşleyin")
    return 0


def _daemon(a) -> int:
    import signal
    import threading

    from .daemon.core import Daemon
    from .daemon.server import PanelServer

    cfg = load_config(a.config)
    d = Daemon(cfg)
    srv = PanelServer(d, a.host, a.port)
    d.start(start_agents=not a.no_agents)
    srv.start()
    print(f"Metin2 QA servisi çalışıyor — panel: {srv.url}  (mod: {cfg.bridge.mode}, ajan: {len(d.agents.agents)})",
          flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    try:
        while not stop.wait(1):
            pass
    finally:
        print("Kapatılıyor…", flush=True)
        srv.shutdown()
        d.shutdown()
    return 0


def _campaign(a) -> int:
    from .daemon.core import Daemon

    cfg = load_config(a.config)
    d = Daemon(cfg)
    d.start()
    try:
        d.agents.wait_online(timeout_s=60)
        params = {"explore": a.explore}
        if a.systems:
            params["systems"] = [x.strip() for x in a.systems.split(",") if x.strip()]
        if a.seed is not None:
            params["seed"] = a.seed
        job = d.jobs.wait(d.jobs.submit("campaign", params, source="cli"), timeout_s=24 * 3600)
        if job["status"] != "done":
            print(f"Kampanya {job['status']}: {job.get('error')}", file=sys.stderr)
            return 2
        c = d.db.get_campaign(job["result"]["campaign_id"])
        rows = []
        for sid, v in c["matrix"].items():
            print(f"{v['status']:9} {v['name']}")
            res = "PASSED" if v["status"] == "passed" else ("FAILED" if v["status"] in ("failed", "findings") else
                                                            "ERROR" if v["status"] == "error" else "SKIPPED")
            rows.append({"scenario": v["name"], "result": res,
                         "summary": ", ".join(f"{r['scenario']}={r['result']}" for r in v["scenarios"]) or "senaryo yok"})
        print(json.dumps(c["summary"], ensure_ascii=False))
        _write_summary(a.summary_md, rows, f"Metin2 QA — kampanya #{c['id']}")
        return 1 if c["summary"]["counts"].get("failed") else 0
    finally:
        d.shutdown()


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
    p.add_argument("--summary-md", help="Sonuç tablosunu bu markdown dosyasına ekle (ör. $GITHUB_STEP_SUMMARY)")
    p = sub.add_parser("affected", help="Değişen dosyalara göre senaryo seç (ve --run ile çalıştır)")
    p.add_argument("files", nargs="*", help="Değişen dosyalar (verilmezse git diff --base)")
    p.add_argument("--base", default="HEAD", help="git diff tabanı (ör. origin/main)")
    p.add_argument("--run", action="store_true")
    p.add_argument("--seed", type=int)
    p.add_argument("--summary-md")
    p = sub.add_parser("explore", help="Otonom keşif ajanı (LLM, varsayılan Gemini)")
    p.add_argument("goal", nargs="?", help="Test hedefi (serbest metin)")
    p.add_argument("--goals", help="Hedef listesi YAML (ör. explore/goals.yaml)")
    p.add_argument("--only", help="--goals içinden yalnızca bu isim")
    p.add_argument("--steps", type=int, help="Oyuncu adımı bütçesi")
    p.add_argument("--save-as", help="Üretilecek regression senaryosunun adı")
    p.add_argument("--setup", help="YAML liste, ör. '- set_gold: 1000'")
    p.add_argument("--seed", type=int)
    p.add_argument("--model", help="LLM modeli (varsayılan: qa.toml [explorer].model / GEMINI_MODEL)")
    p.add_argument("--summary-md")
    p.add_argument("--fail-on-findings", action="store_true", help="bug/major/critical bulguda çıkış kodu 1")
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
    p = sub.add_parser("llm-usage", help="LLM bütçesi: bugün/bu ay kullanım, tavanlar, günlük geçmiş")
    p.add_argument("--days", type=int, default=14)
    p = sub.add_parser("sim-server", help="Simülatörü TCP'de sun")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("mcp", help="MCP sunucusunu stdio'da başlat")
    p = sub.add_parser("packets", help="Headless client için paket profili (packet.h'dan)")
    psub = p.add_subparsers(dest="packets_cmd", required=True)
    pi = psub.add_parser("import", help="packet.h + boyut tablolarından profil üret")
    pi.add_argument("--packet-h", action="append", required=True, help="packet.h (birden çok verilebilir)")
    pi.add_argument("--size-table", action="append", default=[],
                    help="Client PythonNetworkStream.cpp ve/veya sunucu packet_info.cpp")
    pi.add_argument("--defines", action="append", default=[], help="Locale_inc.h / CommonDefines.h / service.h")
    pi.add_argument("--define", action="append", default=[], help="Ek define (AD veya AD=DEGER)")
    pi.add_argument("--name", default="metin2re")
    pi.add_argument("-o", "--output", required=True)
    pinfo = psub.add_parser("info", help="Profil özeti ve headless client uyumluluğu")
    pinfo.add_argument("profile")
    p = sub.add_parser("daemon", help="7/24 QA servisi + web panel")
    p.add_argument("--host", help="Panel adresi (varsayılan qa.toml [daemon].host)")
    p.add_argument("--port", type=int)
    p.add_argument("--no-agents", action="store_true", help="Ajanları otomatik başlatma")
    p = sub.add_parser("campaign", help="Her şeyi test et (daemon olmadan, tek seferlik)")
    p.add_argument("--systems", help="Virgülle ayrılmış sistem id'leri")
    p.add_argument("--explore", action="store_true", help="LLM ile her sistemde keşif de yap")
    p.add_argument("--seed", type=int)
    p.add_argument("--summary-md")

    a = ap.parse_args(argv)

    if a.cmd == "sim-server":
        from .sim.server import main as sim_main

        sim_main(a.rest)
        return 0
    if a.cmd == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return 0

    if a.cmd == "daemon":
        return _daemon(a)
    if a.cmd == "packets":
        return _packets(a)
    if a.cmd == "campaign":
        return _campaign(a)

    from .service import QaService

    s = QaService(load_config(a.config))
    if a.cmd == "list":
        for sc in s.list_scenarios():
            print(f"{sc['name']:28} {','.join(sc.get('tags', [])):28} {sc.get('description') or sc.get('error', '')}")
        return 0
    if a.cmd == "run":
        if a.all or a.tag:
            results = s.run_suite(a.tag, a.seed)["results"]
        elif a.names:
            results = []
            for n in a.names:
                rep = s.run_scenario(n, a.seed)
                results.append({"scenario": n, "run_id": rep["run_id"], "result": rep["result"],
                                "summary": rep["summary"]})
        else:
            ap.error("senaryo adı veya --all gerekli")
        for r in results:
            print(r["summary"])
        _write_summary(a.summary_md, results, f"Metin2 QA — seed {a.seed if a.seed is not None else 'rastgele'}")
        return 0 if all(r["result"] == "PASSED" for r in results) else 1
    if a.cmd == "affected":
        res = s.run_affected(a.files or None, a.base, a.seed, dry_run=not a.run)
        print(f"Değişen: {len(res['changed_files'])} dosya, seçilen: {len(res['selected'])} senaryo")
        for n in res["selected"]:
            print(f"  + {n}: {'; '.join(res['reasons'][n][:3])}")
        if res["unmatched_files"]:
            print(f"  (kurala uymayan: {', '.join(res['unmatched_files'][:10])})")
        for r in res.get("results", []):
            print(r["summary"])
        if res.get("ran"):
            _write_summary(a.summary_md, res["results"], "Metin2 QA — değişikliğe göre seçilen testler", res)
        return 0 if res["ok"] else 1
    if a.cmd == "explore":
        from .planner.llm import LLMError

        try:
            return _explore(s, a, ap)
        except LLMError as e:
            print(f"Hata: {e}", file=sys.stderr)
            return 2
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
    if a.cmd == "llm-usage":
        u = s.llm_usage(a.days)
        t, m, lim = u["today"], u["month"], u["limits"]
        tier = "ücretsiz katman" if u["free_tier"] else "ücretli"
        print(f"Bugün : {t['requests']} istek / {lim['daily_requests'] or '∞'} · {t['total_tokens']:,} token "
              f"({t['cached_tokens']:,} önbellekten) · ${t['cost_usd']:.4f} ({tier}; ücretli olsaydı ${t['list_cost_usd']:.4f})")
        print(f"Bu ay: {m['requests']} istek · {m['total_tokens']:,} token · ${m['cost_usd']:.4f} "
              f"(ücretli olsaydı ${m['list_cost_usd']:.4f})")
        if u["blocked"]:
            print(f"DURDU: {u['blocked']}")
        for d in u["daily"]:
            print(f"  {d['day']}  {d['requests']:5} istek  {d['input_tokens'] + d['output_tokens']:>12,} token  "
                  f"${d['cost_usd']:.4f} (liste ${d['list_cost_usd']:.4f})")
        return 0
    if a.cmd == "reference":
        _print(s.reference())
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
