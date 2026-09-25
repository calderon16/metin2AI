"""Metin2 QA MCP sunucusu — Claude'un kullandığı tool'lar.

    python -m qa.mcp_server          # stdio (Claude Code / Claude Desktop)

Güvenlik: serbest shell veya SQL tool'u yoktur. Build/sunucu komutları yalnızca qa.toml
beyaz listesinden çalışır; hesap işlemleri yalnızca AI_QA_* hesaplarına izinlidir;
production ortamında sunucu hiç açılmaz.

Tipik döngü: build → start_test_server → run_scenario / run_suite → (FAILED) get_test_result,
get_trace, get_server_logs, get_screenshot → kodu düzelt → build → aynı senaryo → replay_failure.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from .daemon.client import DaemonClient
from .service import QaService

INSTRUCTIONS = """Metin2 AI QA Player. Gerçek bir QA karakteriyle (AI_QA_*) oyuna girip sistemleri
normal oyuncu yolundan (login → paket → sunucu → DB) test eder.
- Senaryo yazmadan önce `qa_reference` ile behaviour/assertion listesini al. Yeni senaryoya test
  ettiği kaynak dosyaları `covers:` ile yaz (ör. covers: ["pet_system.cpp"]) ki run_affected onu seçsin.
- Değişiklikten sonra: build → run_affected (değişen dosyalara göre) veya run_suite → FAILED ise get_test_result + get_trace +
  get_server_logs + get_screenshot ile kök nedeni bul → düzelt → tekrar çalıştır.
- Hata gerçek mi? replay_failure(run_id) aynı seed ile tekrar oynatır ("REPRODUCED 3/3").
- Serbest keşif: explore_start(goal) → explore_step(...) döngüsü → explore_finish(findings, save_as_scenario).
- Setup (item/yang verme) sadece senaryonun `setup` bölümünde; adımlar gerçek oyuncu aksiyonlarıdır.
- Çoklu ajan (trade/party): senaryoda `agents: {A: {account: AI_QA_001}, B: {account: AI_QA_002}}`,
  adım/assert'lerde `agent: B`. Takas gibi sistemlerde `conservation` assertion'ı ile toplam item/yang
  korunumunu kontrol et (kopyalama/kayıp).
- 7/24 servis (QA_DAEMON_URL tanımlıysa): run_* tool'ları işi sunucudaki sürekli açık ajanlara gönderir ve
  sonucu bekler; list_agents, submit_job/get_job, run_campaign ("her şeyi test et"), get_findings,
  get_campaign_report ile panelde görülen her şeye erişilir."""

mcp = FastMCP("metin2-qa", instructions=INSTRUCTIONS)
_svc: QaService | None = None


def svc() -> QaService:
    global _svc
    if _svc is None:
        _svc = QaService()
    return _svc


def daemon() -> DaemonClient | None:
    """QA_DAEMON_URL tanımlıysa işler 7/24 servisin ajanlarında çalışır."""
    return DaemonClient.from_env()


def _need_daemon() -> DaemonClient:
    d = daemon()
    if d is None:
        raise ValueError("Bu tool 7/24 servis gerektirir: QA_DAEMON_URL (ve gerekiyorsa QA_PANEL_TOKEN) ayarlayın "
                         "ve sunucuda `metin2-qa daemon` çalıştırın")
    return d


def _job_result(d: DaemonClient, job: dict[str, Any], wait_s: float) -> dict[str, Any]:
    j = d.wait(job["job_id"], wait_s)
    out = {"job_id": j["job_id"], "status": j["status"], "result": j.get("result"), "error": j.get("error"),
           "run_ids": j.get("run_ids"), "agents": j.get("agents")}
    if j.get("note"):
        out["note"] = j["note"]
    return out


# ---------------------------------------------------------------------- build / sunucu

@mcp.tool()
def build(target: str | None = None) -> dict[str, Any]:
    """qa.toml'daki build hedefini (veya hepsini sırayla) derler. Hata satırları ve çıktının sonu döner."""
    return svc().build(target)


@mcp.tool()
def start_test_server() -> dict[str, Any]:
    """QA test sunucusunu başlatır (qa.toml [server].start)."""
    return svc().start_test_server()


@mcp.tool()
def stop_test_server() -> dict[str, Any]:
    """QA test sunucusunu durdurur (qa.toml [server].stop)."""
    return svc().stop_test_server()


# ---------------------------------------------------------------------- senaryolar

@mcp.tool()
def qa_reference() -> dict[str, Any]:
    """Senaryo/keşif için kullanılabilir behaviour'lar, assertion'lar, setup işlemleri (parametreleriyle)."""
    return svc().reference()


@mcp.tool()
def list_scenarios(tag: str | None = None) -> list[dict[str, Any]]:
    """Kayıtlı YAML senaryolarını listeler (isteğe bağlı etiket filtresi)."""
    return svc().list_scenarios(tag)


@mcp.tool()
def get_scenario(name: str) -> str:
    """Senaryonun YAML içeriğini döndürür."""
    return svc().get_scenario(name)


@mcp.tool()
def write_scenario(name: str, yaml_text: str, overwrite: bool = False) -> dict[str, Any]:
    """Yeni regression senaryosu kaydeder. YAML şemaya göre doğrulanır; 'name' alanı dosya adıyla aynı olmalı."""
    return svc().write_scenario(name, yaml_text, overwrite)


# ---------------------------------------------------------------------- çalıştırma

@mcp.tool()
def run_scenario(name: str, seed: int | None = None) -> dict[str, Any]:
    """Senaryoyu gerçek QA karakteriyle çalıştırır; yapılandırılmış raporu (failure, evidence ...) döner.
    7/24 servis varsa sunucudaki boş ajanlarda çalışır."""
    d = daemon()
    if d is not None:
        res = _job_result(d, d.submit("scenario", {"name": name, "seed": seed}), 900)
        if res["status"] == "done" and res.get("result", {}).get("run_id"):
            rep = d.run(res["result"]["run_id"])
            res["report"] = {k: v for k, v in rep.items() if k not in {"assertions", "steps"}}
        return res
    rep = svc().run_scenario(name, seed)
    # Adım ve assertion listelerini kısalt; tamamı get_test_result'ta
    return {k: v for k, v in rep.items() if k not in {"assertions", "steps"}} | {
        "steps": [{k: s.get(k) for k in ("index", "name", "status", "error")} for s in rep["steps"]]}


@mcp.tool()
def run_suite(tag: str | None = None, seed: int | None = None) -> dict[str, Any]:
    """Tüm senaryoları (veya bir etikettekileri) çalıştırır — regression testi."""
    d = daemon()
    if d is not None:
        return _job_result(d, d.submit("suite", {"tag": tag, "seed": seed}), 3600)
    return svc().run_suite(tag, seed)


@mcp.tool()
def run_affected(changed_files: list[str] | None = None, base: str = "HEAD", seed: int | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    """Değişen dosyalara göre sadece ilgili senaryoları çalıştırır. changed_files verilmezse kaynak
    repoda `git diff <base>` + izlenmeyen dosyalar kullanılır. Her senaryo için seçilme nedeni döner.
    dry_run=true: sadece hangi senaryoların seçileceğini göster."""
    d = daemon()
    if d is not None and not dry_run:
        return _job_result(d, d.submit("affected", {"files": changed_files, "base": base, "seed": seed}), 3600)
    return svc().run_affected(changed_files, base, seed, dry_run)


@mcp.tool()
def replay_failure(run_id: str, times: int = 3) -> dict[str, Any]:
    """Run'ı aynı senaryo + aynı seed ile N kez tekrar oynatır; 'REPRODUCED k/N' ve trace determinizmi döner."""
    d = daemon()
    if d is not None:
        return _job_result(d, d.submit("replay", {"run_id": run_id, "times": times}), 1800)
    return svc().replay_failure(run_id, times)


# ---------------------------------------------------------------------- sonuçlar

@mcp.tool()
def get_test_runs(limit: int = 20, status: str | None = None, scenario: str | None = None) -> list[dict[str, Any]]:
    """Son run'lar (status: PASSED/FAILED/ERROR/RUNNING)."""
    d = daemon()
    return d.runs(limit, status, scenario) if d is not None else svc().get_test_runs(limit, status, scenario)


@mcp.tool()
def get_test_result(run_id: str) -> dict[str, Any]:
    """Run'ın tam raporu: adımlar, tüm assertion'lar, failure (expected/actual), build, kanıt dosyaları."""
    d = daemon()
    return d.run(run_id) if d is not None else svc().get_test_result(run_id)


@mcp.tool()
def get_failed_tests(limit: int = 10) -> list[dict[str, Any]]:
    """Son başarısız/hatalı run'lar ve hata detayları."""
    d = daemon()
    if d is not None:
        return d.findings("new,confirmed,flaky,regressed,not_reproduced", limit)
    return svc().get_failed_tests(limit)


@mcp.tool()
def get_trace(run_id: str, tail: int = 200) -> str:
    """Action trace: oyun zamanı, adım, aksiyon, sonuç ve istemci olayları (son N satır)."""
    d = daemon()
    return d.trace(run_id, tail) if d is not None else svc().get_trace(run_id, tail)


@mcp.tool()
def get_server_logs(run_id: str, tail: int = 200) -> str:
    """Run süresince sunucu QA olayları, SYSERR/QA_ASSERT ve yapılandırılmış log dosyaları."""
    d = daemon()
    return d.logs(run_id, tail)["server"] if d is not None else svc().get_server_logs(run_id, tail)


@mcp.tool()
def get_client_logs(run_id: str, tail: int = 200) -> str:
    """Run süresince istemci logları."""
    d = daemon()
    return d.logs(run_id, tail)["client"] if d is not None else svc().get_client_logs(run_id, tail)


@mcp.tool()
def get_screenshot(run_id: str, name: str | None = None) -> Image:
    """Run ekran görüntüsü (isim verilmezse sonuncusu; hata anında otomatik alınır)."""
    return Image(path=str(svc().screenshot_path(run_id, name)))


# ---------------------------------------------------------------------- canlı / hesap

@mcp.tool()
def get_player_state(account: str | None = None) -> dict[str, Any]:
    """QA karakterinin anlık durumu: oyuncu, envanter, hedef, yakın varlıklar, görevler, UI, mesajlar."""
    return svc().get_player_state(account)


@mcp.tool()
def reset_test_account(account: str) -> dict[str, Any]:
    """AI_QA_* hesabını başlangıç durumuna döndürür (sunucudaki /qa reset; DB'ye doğrudan yazılmaz)."""
    return svc().reset_test_account(account)


# ---------------------------------------------------------------------- keşif

@mcp.tool()
def explore_start(goal: str, account: str | None = None, seed: int | None = None,
                  setup: list[Any] | None = None) -> dict[str, Any]:
    """Serbest keşif oturumu açar. Döner: run_id, başlangıç durumu, yakın varlıklar, edge-case checklist.
    setup örneği: [{"set_gold": 1000}, {"give_item": {"vnum": 27001, "count": 5}}]"""
    return svc().explorer.start(goal, account, seed, setup)


@mcp.tool()
def explore_step(run_id: str, step: Any) -> dict[str, Any]:
    """Keşifte tek behaviour adımı yürütür. step: senaryo adımıyla aynı biçim, ör.
    {"kill_monster": {"vnum": 101, "count": 2}} veya {"buy_item": {"vnum": 27001}, "expect_error": "NOT_ENOUGH_GOLD"}.
    Döner: sonuç, durum farkı (diff), istemci olayları, yeni sunucu hataları, açık pencereler."""
    return svc().explorer.step(run_id, step)


@mcp.tool()
def explore_check(run_id: str, asserts: list[Any]) -> dict[str, Any]:
    """Keşif sırasında assertion'ları değerlendirir, ör. [{"gold": {"min": 0}}, "alive"]."""
    return svc().explorer.check(run_id, asserts)


@mcp.tool()
def explore_observe(run_id: str) -> dict[str, Any]:
    """Keşif oturumunun anlık durumu (oyuncu, envanter, görevler, pencereler, yakın varlıklar)."""
    return svc().explorer.observe(run_id)


@mcp.tool()
def explore_finish(run_id: str, findings: list[dict[str, Any]] | None = None,
                   save_as_scenario: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Keşfi bitirir ve raporlar. findings: [{"title","description","severity":"bug|minor|note","step",
    "expected","actual"}]. save_as_scenario verilirse yürütülen adımlar + check'ler regression senaryosu olur."""
    return svc().explorer.finish(run_id, findings, save_as_scenario, overwrite)


@mcp.tool()
def explore_autonomous(goal: str, max_steps: int | None = None, save_as_scenario: str | None = None,
                       setup: list[Any] | None = None, seed: int | None = None) -> dict[str, Any]:
    """Otonom keşif ajanını (qa.toml [explorer], varsayılan Gemini) çalıştırır: hedefe göre oyunu kendi
    başına oynar, edge-case arar, bulguları raporlar. save_as_scenario verilirse yürütülen başarılı adımlar
    + kontroller regression senaryosu olarak kaydedilir ve bir kez doğrulama için çalıştırılır.
    Uzun sürebilir (adım sayısıyla orantılı). API anahtarı ortam değişkeninde olmalı (GEMINI_API_KEY)."""
    d = daemon()
    if d is not None:
        return _job_result(d, d.submit("explore", {"goal": goal, "max_steps": max_steps, "save_as": save_as_scenario,
                                                   "setup": setup, "seed": seed}), 3600)
    return svc().explore_auto(goal, max_steps, save_as_scenario, setup, seed)


# ---------------------------------------------------------------------- 7/24 servis (daemon)

@mcp.tool()
def service_status() -> dict[str, Any]:
    """7/24 QA servisinin durumu: ajan sayıları, kuyruk, açık bulgular, son kampanya, LLM."""
    return _need_daemon().status()


@mcp.tool()
def list_agents() -> list[dict[str, Any]]:
    """Sunucuda sürekli açık AI oyuncular: durum, HP/harita/konum, çalıştığı iş, son hata."""
    return _need_daemon().agents()


@mcp.tool()
def submit_job(type: str, params: dict[str, Any] | None = None, wait: bool = False,
               wait_s: float = 600) -> dict[str, Any]:
    """Servise iş gönder. type: scenario | suite | affected | explore | campaign | replay | confirm.
    wait=true ise bitmesini bekler (en fazla wait_s)."""
    d = _need_daemon()
    job = d.submit(type, params or {})
    return _job_result(d, job, wait_s) if wait else job


@mcp.tool()
def get_job(job_id: int) -> dict[str, Any]:
    """İşin durumu, ilerlemesi, run'ları ve sonucu."""
    return _need_daemon().job(job_id)


@mcp.tool()
def run_campaign(systems: list[str] | None = None, explore: bool | None = None, wait: bool = True,
                 wait_s: float = 7200) -> dict[str, Any]:
    """"Her şeyi test et": katalogdaki her sistem (veya verilenler) senaryolarıyla (+ LLM varsa keşifle)
    test edilir. Sonuç: sistem × durum matrisi, yeni kırmızılar, düzelenler."""
    d = _need_daemon()
    params: dict[str, Any] = {}
    if systems:
        params["systems"] = systems
    if explore is not None:
        params["explore"] = explore
    job = d.submit("campaign", params)
    if not wait:
        return job
    res = _job_result(d, job, wait_s)
    cid = (res.get("result") or {}).get("campaign_id")
    if cid:
        c = d.campaign(cid)
        res["matrix"] = {k: {"status": v["status"], "scenarios": [(r["scenario"], r["result"], r["run_id"])
                                                                  for r in v["scenarios"]]}
                         for k, v in (c.get("matrix") or {}).items()}
    return res


@mcp.tool()
def get_findings(status: str | None = "new,confirmed,flaky,regressed,not_reproduced",
                 limit: int = 50) -> list[dict[str, Any]]:
    """Tekilleştirilmiş bulgular (durum: new, confirmed, flaky, not_reproduced, regressed, fixed, ignored)."""
    return _need_daemon().findings(status, limit)


@mcp.tool()
def update_finding(finding_id: int, status: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Bulgunun durumunu/notunu güncelle (ör. düzeltme commit'ini not olarak yaz)."""
    return _need_daemon().update_finding(finding_id, status, note)


@mcp.tool()
def get_campaign_report(campaign_id: int | None = None) -> dict[str, Any]:
    """Kampanya raporu (verilmezse sonuncusu): sistem matrisi, regresyonlar, düzelenler."""
    d = _need_daemon()
    if campaign_id is None:
        items = d.campaigns(1)
        if not items:
            return {"note": "Henüz kampanya yok"}
        campaign_id = items[0]["id"]
    return d.campaign(campaign_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
