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

from .service import QaService

INSTRUCTIONS = """Metin2 AI QA Player. Gerçek bir QA karakteriyle (AI_QA_*) oyuna girip sistemleri
normal oyuncu yolundan (login → paket → sunucu → DB) test eder.
- Senaryo yazmadan önce `qa_reference` ile behaviour/assertion listesini al. Yeni senaryoya test
  ettiği kaynak dosyaları `covers:` ile yaz (ör. covers: ["pet_system.cpp"]) ki run_affected onu seçsin.
- Değişiklikten sonra: build → run_affected (değişen dosyalara göre) veya run_suite → FAILED ise get_test_result + get_trace +
  get_server_logs + get_screenshot ile kök nedeni bul → düzelt → tekrar çalıştır.
- Hata gerçek mi? replay_failure(run_id) aynı seed ile tekrar oynatır ("REPRODUCED 3/3").
- Serbest keşif: explore_start(goal) → explore_step(...) döngüsü → explore_finish(findings, save_as_scenario).
- Setup (item/yang verme) sadece senaryonun `setup` bölümünde; adımlar gerçek oyuncu aksiyonlarıdır."""

mcp = FastMCP("metin2-qa", instructions=INSTRUCTIONS)
_svc: QaService | None = None


def svc() -> QaService:
    global _svc
    if _svc is None:
        _svc = QaService()
    return _svc


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
    """Senaryoyu gerçek QA karakteriyle çalıştırır; yapılandırılmış raporu (failure, evidence ...) döner."""
    rep = svc().run_scenario(name, seed)
    # Adım ve assertion listelerini kısalt; tamamı get_test_result'ta
    return {k: v for k, v in rep.items() if k not in {"assertions", "steps"}} | {
        "steps": [{k: s.get(k) for k in ("index", "name", "status", "error")} for s in rep["steps"]]}


@mcp.tool()
def run_suite(tag: str | None = None, seed: int | None = None) -> dict[str, Any]:
    """Tüm senaryoları (veya bir etikettekileri) çalıştırır — regression testi."""
    return svc().run_suite(tag, seed)


@mcp.tool()
def run_affected(changed_files: list[str] | None = None, base: str = "HEAD", seed: int | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    """Değişen dosyalara göre sadece ilgili senaryoları çalıştırır. changed_files verilmezse kaynak
    repoda `git diff <base>` + izlenmeyen dosyalar kullanılır. Her senaryo için seçilme nedeni döner.
    dry_run=true: sadece hangi senaryoların seçileceğini göster."""
    return svc().run_affected(changed_files, base, seed, dry_run)


@mcp.tool()
def replay_failure(run_id: str, times: int = 3) -> dict[str, Any]:
    """Run'ı aynı senaryo + aynı seed ile N kez tekrar oynatır; 'REPRODUCED k/N' ve trace determinizmi döner."""
    return svc().replay_failure(run_id, times)


# ---------------------------------------------------------------------- sonuçlar

@mcp.tool()
def get_test_runs(limit: int = 20, status: str | None = None, scenario: str | None = None) -> list[dict[str, Any]]:
    """Son run'lar (status: PASSED/FAILED/ERROR/RUNNING)."""
    return svc().get_test_runs(limit, status, scenario)


@mcp.tool()
def get_test_result(run_id: str) -> dict[str, Any]:
    """Run'ın tam raporu: adımlar, tüm assertion'lar, failure (expected/actual), build, kanıt dosyaları."""
    return svc().get_test_result(run_id)


@mcp.tool()
def get_failed_tests(limit: int = 10) -> list[dict[str, Any]]:
    """Son başarısız/hatalı run'lar ve hata detayları."""
    return svc().get_failed_tests(limit)


@mcp.tool()
def get_trace(run_id: str, tail: int = 200) -> str:
    """Action trace: oyun zamanı, adım, aksiyon, sonuç ve istemci olayları (son N satır)."""
    return svc().get_trace(run_id, tail)


@mcp.tool()
def get_server_logs(run_id: str, tail: int = 200) -> str:
    """Run süresince sunucu QA olayları, SYSERR/QA_ASSERT ve yapılandırılmış log dosyaları."""
    return svc().get_server_logs(run_id, tail)


@mcp.tool()
def get_client_logs(run_id: str, tail: int = 200) -> str:
    """Run süresince istemci logları."""
    return svc().get_client_logs(run_id, tail)


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
