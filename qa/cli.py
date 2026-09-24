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
    if a.cmd == "reference":
        _print(s.reference())
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
