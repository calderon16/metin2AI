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

Gemini günlük kotası dolduğunda yerel Codex CLI yedek keşif ajanı olarak kullanılabilir.
`[explorer] fallback_provider = "codex_cli"` ayarını açmadan önce `codex login status` ile
CLI oturumunu doğrulayın. Codex yalnız yapılandırılmış QA aracı çağrıları önerir; oyun
işlemlerini QA motoru gerçekleştirir. CLI kullanımı Gemini'nin ücretsiz API kotasından
ayrıdır; kullanım ve olası abonelik sınırları Codex hesabına bağlıdır.

```text
[Web panel]  [Claude (MCP)]
      │  HTTP/JSON  │
      ▼             ▼
 metin2-qa daemon — test sunucusunun VM'inde 7/24
 ├─ Ajanlar: AI_QA_001..N sürekli oyunda ("online tut", kopunca yeniden bağlanır)
 ├─ İş kuyruğu + zamanlayıcı: senaryo / suite / değişen dosyalar / AI keşfi / "her şeyi test et"
 ├─ Bulgular: tekilleştirme, replay ile doğrulama, düzeldi/geriledi takibi
 └─ Kampanya: 21 sistemlik katalog → sistem sağlık matrisi
      │
 Behaviour Engine (walk_to, kill_monster, trade_with ...) + Bug Oracle
      │  bridge sözleşmesi (cmd_*)
      ├─ headless  : ekransız paket client → auth/game sunucusuna GERÇEK paketlerle (client gerekmez)
      ├─ sim       : deterministik simülatör (kaynak kodu olmadan geliştirme/test)
      └─ tcp       : Metin2_QA.exe + QaBridge (görsel/UI testleri için, opsiyonel)
      ▼
 game/db/auth + integration/server/* (QA_EVENT, QA_ASSERT, /qa hazırlık komutları)
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
| `trade_item_dupe` | `trade_item_for_gold` (conservation: item kopyalandı) |
| `trade_gold_dupe` | `trade_item_for_gold` (conservation: yang kopyalandı) |
| `trade_accept_not_reset` | `trade_change_after_accept` (onay sıfırlanmadı → dolandırıcılık) |
| `party_exp_dupe` | `party_exp_share` (exp paylaşımı) |

## 7/24 servis ve web panel

AI oyuncular test sunucusunun VM'inde sürekli açık durur; oyun client'ı açmak gerekmez. Onları web
panelden yönetirsin:

```bash
QA_PANEL_TOKEN=gizli metin2-qa daemon          # panel: http://127.0.0.1:8765
```

- **Genel bakış:** ajan kartları (online/meşgul/koptu, HP, yang, konum, çalıştığı iş), sistem sağlık
  matrisi, son işler, açık bulgular.
- **Görev ver:** "Her şeyi test et" kampanyası, tek senaryo, etiket suite'i, değişen dosyalar, serbest AI
  keşfi (Gemini anahtarı varsa).
- **Run raporu:** adımlar, oracle kontrolleri (beklenen/gerçekleşen), ekran görüntüleri, action trace,
  sunucu/client logları, "tekrar oynat".
- **Bulgular:** aynı hata tekrar görülünce yeni kayıt açılmaz. Yeni bulgu otomatik 3 kez tekrar oynatılır
  ("doğrulandı 3/3" / "kararsız"). Senaryo tekrar geçince "düzeldi", tekrar bozulursa "geriledi" olur.
- **Kampanyalar:** `catalog/systems.yaml`'daki her sistem test edilir. Önceki kampanyaya göre yeni
  kırmızılar işaretlenir. Senaryosu olmayan sistemler "test edilmedi" olarak görünür.
- **Zamanlama:** `[[daemon.schedules]]` ile örneğin her gece 03:00'te kampanya, saatlik smoke ya da
  `continuous` (ajanlar boşta kaldıkça sürekli oynar).

Kurulum (FreeBSD rc.d / Linux systemd / Windows): [deploy/README.md](deploy/README.md).

## Headless mod: oyun client'ı olmadan gerçek oyuncu

`[bridge] mode = "headless"` ile ajanlar Python'daki ekransız bir Metin2 client'ıyla auth/game
sunucusuna bağlanır ve gerçek client'ın gönderdiği paketlerin aynısını gönderir: login, karakter seçimi,
insan hızında adım adım hareket, saldırı, item, NPC/görev diyaloğu, chat. Paket numaraları ve yapıları
fork'tan fork'a değiştiği için elle yazılmaz, **kaynaktan otomatik çıkarılır**:

```bash
metin2-qa packets import --packet-h <kaynak>/common/packet.h \
    --size-table <client>/UserInterface/PythonNetworkStream.cpp \
    --size-table <server>/game/src/packet_info.cpp \
    --defines <client>/UserInterface/Locale_inc.h -o profiles/metin2re.json
metin2-qa packets info profiles/metin2re.json      # hangi mesajlar eşlendi, hangileri eksik
```

Fork'ta adı farklı olan paket/alanlar `profiles/metin2re.bindings.yaml` ile eşlenir. Ayrıntılar:
[integration/INTEGRATION.md](integration/INTEGRATION.md#0-headless-mod-önerilen).

## Claude ile kullanım (MCP)

`.mcp.json` repoda hazır; Claude Code bu klasörde açıldığında `metin2-qa` sunucusunu görür.
Claude Desktop için aynı komutu (`.venv/bin/python -m qa.mcp_server`, `QA_CONFIG=qa.toml`) ekleyin.

| Tool | Ne yapar |
|---|---|
| `build`, `start_test_server`, `stop_test_server` | qa.toml'daki whitelist komutları |
| `qa_reference` | behaviour / assertion / setup listesi (senaryo yazmak için) |
| `list_scenarios`, `get_scenario`, `write_scenario` | senaryo yönetimi (şema doğrulamalı) |
| `run_scenario`, `run_suite` | test çalıştır, yapılandırılmış rapor al |
| `run_affected` | sadece değişen dosyaların etkilediği senaryolar (seçilme nedenleriyle) |
| `get_test_runs`, `get_test_result`, `get_failed_tests` | sonuçlar |
| `get_trace`, `get_server_logs`, `get_client_logs`, `get_screenshot` | kanıt |
| `replay_failure` | aynı seed ile tekrar → `REPRODUCED k/N` |
| `get_player_state`, `reset_test_account` | canlı durum, QA hesabı sıfırlama |
| `explore_start`, `explore_step`, `explore_check`, `explore_observe`, `explore_finish` | serbest keşif; bitince senaryo olarak kaydedilebilir |
| `explore_autonomous` | otonom keşif ajanı (Gemini) — hedefi ver, kendi başına test etsin |
| `service_status`, `list_agents`, `submit_job`, `get_job` | 7/24 servis (`QA_DAEMON_URL` tanımlıyken) |
| `run_campaign`, `get_campaign_report`, `get_findings`, `update_finding` | "her şeyi test et" ve bulgu yönetimi |

`QA_DAEMON_URL` (ve `QA_PANEL_TOKEN`) tanımlıysa `run_*` tool'ları işi sunucudaki sürekli açık ajanlara
gönderir; Claude'un çalıştırdığı her şey panelde de görünür.

Kapalı döngü örneği:

```text
Sen:    "Köpek avı görevinde ödül iki kez veriliyor, düzelt."
Claude: build → run_scenario("quest_dog_hunt") → FAILED → get_test_result + get_trace +
        get_server_logs → questlua'yı düzeltir → build → run_scenario → PASSED →
        replay_failure(eski run) → NOT REPRODUCED 0/3 → run_suite (regression)
```

Güvenlik: serbest shell ya da SQL tool'u yoktur; build/sunucu komutları yalnızca `qa.toml`'dan,
hesap işlemleri yalnızca `AI_QA_*` hesaplarına; `env = "production"` iken sistem çalışmaz.

## Değişikliğe göre test seçimi

Claude `shop.cpp`'yi değiştirdiyse bütün suite yerine yalnızca dükkan senaryoları koşar:

```bash
metin2-qa affected game/src/shop.cpp            # sadece seçimi göster
metin2-qa affected --base origin/main --run     # git diff'e göre seç ve çalıştır
#   + npc_shop_buy_sell: game/src/shop.cpp ~ covers 'shop.cpp'; ... ~ kural 'shop*.cpp' → shop
#   + smoke_login_walk: always → smoke
```

Seçim üç kaynaktan gelir: senaryonun `covers:` alanı (test ettiği dosyalar), `qa.toml`
`[selection.rules]` (klasik Metin2 dosya adları için varsayılanlar hazır: `exchange*.cpp → trade`,
`questlua*.cpp → quest` ...) ve eşleşmeyen dosyalar için `fallback`. `always` her seçime eklenir.

## CI (GitHub Actions)

`.github/workflows/qa.yml` her push/PR'da çalışır:

- **pytest** (Python 3.11 ve 3.12)
- **tüm senaryolar** iki sabit seed ile, sonuç tablosu iş özetinde, kanıtlar artifact olarak
- **PR'larda** yalnızca değişikliğin etkilediği senaryolar (`affected --base origin/<hedef>`)
- **her gece** rastgele seed ile tüm senaryolar (seed'e bağlı gizli hatalar için; başarısız run'ın
  seed'i raporda olduğundan `replay` ile birebir tekrarlanır)

## Çoklu ajan (trade, party)

Birden fazla gerçek QA karakteri aynı senaryoda. Adımlar sırayla, ilgili ajanın istemcisinden yürür;
assert'ler ajan bazında ya da tüm ajanlar üzerinden değerlendirilir.

```yaml
name: trade_item_for_gold
agents:
  A: {account: AI_QA_001}
  B: {account: AI_QA_002}
setup:
  - give_item: {vnum: 10, count: 1}
    agent: A
  - set_gold: 500
    agent: B
steps:
  - trade_with: {agent: B}
    agent: A
  - trade_add_item: {vnum: 10}
    agent: A
  - trade_set_gold: 300
    agent: B
  - trade_accept:
    agent: A
  - trade_accept:
    agent: B
assert:
  - gold: {equals: 300}
    agent: A
  - conservation: {gold: true, vnums: [10]}   # toplam item/yang korunmalı: kopyalama/kayıp yok
```

Behaviour'lar: `trade_with, trade_add_item, trade_set_gold, trade_accept, trade_cancel,
party_invite, party_accept, party_decline, party_leave, party_kick`. Assertion'lar: `conservation`,
`trade`, `party`. Hazır senaryolar: `trade_item_for_gold`, `trade_change_after_accept`
(onaydan sonra teklif değiştirme dolandırıcılığı), `trade_inventory_full`, `party_exp_share`.
Gerçek client'ta her ajan ayrı `Metin2_QA.exe` örneğidir (`[bridge.agent_ports]`).

## Otonom keşif ajanı (Gemini)

### LLM maliyeti ve bütçe

Keşif çağrıları `artifacts/qa.sqlite` içindeki `llm_usage` tablosuna istek, girdi/çıktı ve
önbellek token'larıyla kaydedilir. `metin2-qa llm-usage` komutu ve panelin genel bakışındaki
**LLM kullanımı** kartı bugün ve bu ayın toplamını gösterir. Ücretsiz katmanda gerçek maliyet
0 USD olarak kaydedilir; yapılandırılan fiyatlarla hesaplanan “ücretli olsaydı” tutarı da görünür.
Bu fiyatlar `qa.toml` içindeki örnek değerlerdir; sağlayıcının güncel fiyatı değişirse düzenleyin.

`[explorer]` ayarlarında `requests_per_minute` hız sınırını, `daily_request_limit` günlük
istek tavanını, isteğe bağlı `daily_token_limit` günlük token tavanını ve ücretli moddaki
`monthly_cost_limit_usd` harcama tavanını belirler. Tavan dolunca yeni keşif işi reddedilir;
devam eden keşif `budget_exhausted` ile durur. Kampanyadaki keşif adımı “atlandı (bütçe)”
olarak işaretlenir. `thinking_budget=0`, `history_turns=10` ve `max_total_tokens=600000`
varsayılanları istem başına token sayısını sınırlar. `qa.local.toml` kullanılıyorsa değişiklikleri
onun yalnız `[explorer]` bölümünde yapın; çalışan servise uygulamak için yönetici onayıyla
yeniden başlatın. API anahtarı ve panel/QA parolaları yapılandırma dosyasına yazılmaz.

Hedefi verirsin, ajan oyunu kendi başına oynar, edge-case arar ve bulgularını raporlar:

```bash
export GEMINI_API_KEY=...            # Google AI Studio'dan; dosyaya yazılmaz
export GEMINI_MODEL=gemini-2.5-flash # opsiyonel (qa.toml [explorer].model da olur)

metin2-qa explore "Genel Mağaza'yı oyuncu gibi kullan; yetersiz yang ve dolu envanteri dene" \
    --steps 40 --save-as auto_shop
metin2-qa explore --goals explore/goals.yaml      # hazır hedef listesi
```

Nasıl çalışır:

- Model behaviour'ları (`kill_monster`, `buy_item`, `talk_npc` ...) **tool** olarak görür (Gemini function
  calling). Ayrıca `observe`, `check` (assertion), `report_finding` ve yalnızca ilk adımdan önce `qa_setup`.
  Shell/SQL/dosya erişimi yoktur.
- Her adımın sonucu modele döner: durum farkı, istemci olayları, yeni SYSERR/QA_ASSERT'ler, oracle hataları.
- Bütçe: adım sayısı, tur sayısı, toplam token (`qa.toml [explorer]`). Uzun keşiflerde bağlam kayan pencereyle tutulur.
- Çıktı: keşif raporu (`agent_summary`, `findings`, token kullanımı, `llm_transcript.jsonl`) ve `--save-as`
  ile **regression senaryosu**. Senaryoya modelin hatalı çağrıları girmez; oracle'ın itiraz ettiği adımlar
  girer. Böylece bulunan bug, düzeltilene kadar FAILED veren bir teste dönüşür. Senaryo üretilince aynı
  seed ile bir kez doğrulama için çalıştırılır.
- MCP'den: `explore_autonomous(goal, max_steps, save_as_scenario)`.
- **Gece CI:** GitHub'da `GEMINI_API_KEY` secret'ı (ve istersen `GEMINI_MODEL` variable'ı) tanımlarsan
  `explore/goals.yaml` her gece çalışır; rapor ve üretilen senaryolar artifact olarak yüklenir. Secret yoksa
  iş atlanır.

Model bağımsızdır: `qa/planner/llm.py` içindeki `LLMProvider` arayüzünü uygulayan başka bir sağlayıcı
(ör. Claude) eklenebilir.

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
qa/planner/    keşif modu, otonom ajan (autonomous.py), LLM sağlayıcıları (llm.py)
explore/       otonom keşif hedefleri
qa/build/      whitelist build/sunucu komutları
qa/mcp_server.py, qa/cli.py, qa/service.py
integration/   gerçek client/sunucu kodu + INTEGRATION.md
scenarios/     örnek senaryolar
qa/selection.py  değişikliğe göre test seçimi
qa/daemon/     7/24 servis: ajanlar, iş kuyruğu, zamanlayıcı, bulgular, kampanya, HTTP API, web/ panel
qa/headless/   ekransız paket client: packet.h profili, çerçeveleme, şifreleme, bindings, sahte sunucu
catalog/       oyun sistemleri kataloğu (kampanya haritası)
deploy/        FreeBSD rc.d, Linux systemd, Windows servis notları
.github/       CI
tests/         pytest (simülatöre karşı)
```

## Yol haritası

Hazır: bridge, test runner, oracle, replay, MCP, keşif, değişikliğe göre seçim, CI, çoklu ajan
(trade/party), otonom keşif ajanı (Gemini), 7/24 servis + web panel, kampanya ve bulgu yönetimi, headless
paket client çerçevesi. Sıradakiler: headless client'ın gerçek sunucuda doğrulanması (kaynak gelince:
şifreleme, dükkan/ticaret/grup paketleri, harita attr ile yol bulma), çoklu ajanlı otonom keşif,
guild/PvP/offline shop
senaryoları, eşzamanlı (race condition) çoklu ajan adımları, gcov ile coverage yönlendirmeli
senaryo üretimi, görsel assertion'lar, QA client'ta kanal değiştirme.
