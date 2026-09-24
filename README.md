# Metin2 AI QA Player

Claude ile entegre, gerçek bir oyuncu karakteriyle oyunu test eden otonom QA sistemi.

```text
Claude kodu yazar → build → QA sunucusu → AI karakter oyuna girer → sistemi dener
      ▲                                                                   │
      └──── Claude raporu okur, düzeltir ◄── rapor + kanıt ◄── Bug Oracle ◄┘
```

AI karakter veritabanına yazarak "sahte test" yapmaz. Her aksiyon gerçek oyuncunun yolundan geçer:
`login → karakter seçimi → hareket → NPC → item → paket → sunucu → DB`.

## Mimari

```text
Claude ──MCP──► qa/mcp_server.py ──► QaService
                                        ├─ ScenarioRunner (YAML senaryo)   ├─ Build Manager (whitelist)
                                        ├─ ExplorationManager (keşif)      ├─ Store (SQLite + artifacts/)
                                        └─ Replay
                                              │
                          Behaviour Engine (walk_to, kill_monster, buy_item ...)  ← seed'li RNG
                                              │  bridge protokolü (JSON-lines / TCP)
                         ┌────────────────────┴───────────────────┐
                    Metin2_QA.exe (gerçek)                qa/sim (simülatör)
                    integration/client/*                  aynı protokol, deterministik
                         │ normal paketler
                    game/db/auth + integration/server/* (QA_EVENT, QA_ASSERT, /qa)
```

- **LLM her tuşa basmaz.** Claude "5 köpek kes" der; `kill_monster` behaviour'ı mob bulur,
  yaklaşır, hedef alır, saldırır, gerekirse iksir içer.
- **Durum yapılandırılmış API'den okunur** (HP, envanter, varlıklar, pencereler); ekran görüntüsü
  birincil kaynak değil, görsel kanıttır (her hatada otomatik alınır).
- **Bug Oracle**: satır içi `expect`, final `assert`, ve her run'da örtük olarak
  "SYSERR yok" + "QA_ASSERT ihlali yok" kontrolleri.
- **Deterministik**: her run bir `seed` ile çalışır; `replay_failure` aynı seed ile tekrar
  oynatır → `REPRODUCED 3/3`.

## Hızlı başlangıç

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip install -e ".[dev]"

.venv/bin/metin2-qa list
.venv/bin/metin2-qa run --all          # tüm senaryolar (simülatöre karşı)
.venv/bin/metin2-qa run quest_dog_hunt --seed 846219
.venv/bin/metin2-qa show QA-2026-00001 --trace
.venv/bin/python -m pytest -q
```

Kaynak kodu olmadan her şey **simülatöre** karşı uçtan uca çalışır (`qa.toml` → `[bridge] mode = "sim"`).
Gerçek oyuna bağlamak için: [integration/INTEGRATION.md](integration/INTEGRATION.md).

### Oracle'ın gerçekten hata yakaladığını görmek

Simülatöre bilinçli hata enjekte edin:

```bash
QA_CONFIG=qa.toml .venv/bin/metin2-qa run quest_dog_hunt   # PASSED
# qa.toml → [sim] faults = ["quest_off_by_one"]
.venv/bin/metin2-qa run quest_dog_hunt
# QA-2026-00002 quest_dog_hunt FAILED: adım 3 (kill_monster) — quest: ASSERTION FAILED
#   beklenen={'progress': 5} gerçekleşen={'state': 'active', 'progress': 4, 'goal': 5}
.venv/bin/metin2-qa replay QA-2026-00002 --times 3
# REPRODUCED 3/3 (deterministic_trace=True)
```

| Fault | Yakalayan senaryo |
|---|---|
| `quest_off_by_one` | `quest_dog_hunt` (görev ilerlemesi) |
| `no_drop` | `kill_mob_pickup` (DROP_NOT_OBTAINED) |
| `sell_no_gold` | `npc_shop_buy_sell` (yang farkı) |
| `potion_no_heal` | `use_equip_item` (HP farkı) |
| `syserr_on_equip` | `use_equip_item` (örtük server_errors) |
| `negative_gold_on_buy` | `shop_insufficient_gold` (expect_error + QA_ASSERT) |

## Claude ile kullanım (MCP)

`.mcp.json` repoda hazır; Claude Code bu klasörde açıldığında `metin2-qa` sunucusunu görür.
Claude Desktop için aynı komutu (`.venv/bin/python -m qa.mcp_server`, `QA_CONFIG=qa.toml`) ekleyin.

| Tool | Ne yapar |
|---|---|
| `build`, `start_test_server`, `stop_test_server` | qa.toml'daki whitelist komutları |
| `qa_reference` | behaviour / assertion / setup listesi (senaryo yazmak için) |
| `list_scenarios`, `get_scenario`, `write_scenario` | senaryo yönetimi (şema doğrulamalı) |
| `run_scenario`, `run_suite` | test çalıştır, yapılandırılmış rapor al |
| `get_test_runs`, `get_test_result`, `get_failed_tests` | sonuçlar |
| `get_trace`, `get_server_logs`, `get_client_logs`, `get_screenshot` | kanıt |
| `replay_failure` | aynı seed ile tekrar → `REPRODUCED k/N` |
| `get_player_state`, `reset_test_account` | canlı durum, QA hesabı sıfırlama |
| `explore_start`, `explore_step`, `explore_check`, `explore_observe`, `explore_finish` | serbest keşif; bitince senaryo olarak kaydedilebilir |

Kapalı döngü örneği:

```text
Sen:    "Köpek avı görevinde ödül iki kez veriliyor, düzelt."
Claude: build → run_scenario("quest_dog_hunt") → FAILED → get_test_result + get_trace +
        get_server_logs → questlua'yı düzeltir → build → run_scenario → PASSED →
        replay_failure(eski run) → NOT REPRODUCED 0/3 → run_suite (regression)
```

Güvenlik: serbest shell ya da SQL tool'u yoktur; build/sunucu komutları yalnızca `qa.toml`'dan,
hesap işlemleri yalnızca `AI_QA_*` hesaplarına; `env = "production"` iken sistem çalışmaz.

## Senaryo formatı

```yaml
name: npc_shop_buy_sell
account: AI_QA_001
seed: 846219                 # opsiyonel; verilmezse rastgele seçilir ve rapora yazılır
setup:                       # sadece burada /qa hazırlık komutları kullanılır
  - set_gold: 300
  - give_item: {vnum: 10, count: 1}
steps:                       # gerçek oyuncu aksiyonları
  - talk_npc: {vnum: 9001}
    expect:
      - window_open: shop
  - buy_item: {vnum: 27001, count: 3}
    expect:
      - gold: {delta: -150}
  - buy_item: {vnum: 11200}
    expect_error: NOT_ENOUGH_GOLD     # edge-case: reddedilmesi bekleniyor
assert:
  - item_count: {vnum: 27001, min: 3}
  - server_errors: 0
```

Tam liste: `metin2-qa reference`. Başlıca behaviour'lar: `walk_to, move_to_entity, kill_monster,
kill_until_drop, pickup, use_item, equip_item, unequip_item, drop_item, talk_npc, select_dialog,
buy_item, sell_item, close_window, wait, wait_for_event, use_skill, respawn, chat, change_channel,
reconnect, screenshot`. Assertion'lar: `inventory_contains, inventory_not_contains, item_count,
equipped, gold, level, hp, player, alive, position_near, quest, window_open, window_closed,
system_message, client_event, server_event, server_errors, qa_asserts` (`equals/min/max/delta`).

## Rapor

Her run `artifacts/<run_id>/` altında: `report.json`, `trace.jsonl` + `trace.txt` (oyun zamanlı
action trace), `client.log`, `server.log`, `server_events.jsonl`, `scenario.yaml` (replay için
anlık görüntü), `screenshots/`. Örnek `report.json` özeti:

```json
{
  "run_id": "QA-2026-00002", "scenario": "quest_dog_hunt", "result": "FAILED", "seed": 846219,
  "build": {"git_commit": "b782fd2", "branch": "feature/pet-v2", "env": "qa"},
  "failure": {"step": 3, "action": "kill_monster", "kind": "assertion", "name": "quest",
              "expected": {"progress": 5}, "actual": {"state": "active", "progress": 4, "goal": 5}},
  "evidence": {"action_trace": "trace.jsonl", "server_log": "server.log",
               "screenshots": ["screenshots/001_failure_step_3.png"]}
}
```

## Proje yapısı

```text
qa/bridge/     protokol + TCP/in-process istemci
qa/sim/        deterministik Metin2 simülatörü (+ TCP sunucusu, PNG ekran görüntüsü)
qa/engine/     seed'li RNG, GameContext + trace, behaviour'lar
qa/scenario/   YAML şema, yükleyici, runner
qa/oracle/     assertion'lar, sunucu sinyalleri (QA_EVENT dosyası, log dosyaları)
qa/store/      SQLite, artifact'ler, rapor
qa/planner/    keşif (explore) modu
qa/build/      whitelist build/sunucu komutları
qa/mcp_server.py, qa/cli.py, qa/service.py
integration/   gerçek client/sunucu kodu + INTEGRATION.md
scenarios/     örnek senaryolar
tests/         pytest (simülatöre karşı)
```

## Yol haritası

Bu sürüm MVP'dir (bridge + test runner + MCP + keşif iskeleti). Sıradakiler: çoklu ajan
(trade/party/guild/PvP — simülatör birden fazla bağlantıyı zaten destekliyor), gcov ile
coverage yönlendirmeli senaryo üretimi, görsel assertion'lar, QA client'ta kanal değiştirme.
