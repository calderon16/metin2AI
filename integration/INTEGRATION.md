# Metin2 kaynağına entegrasyon

Bu klasördeki dosyalar, orchestrator'ı (bu repodaki `qa/` paketi) **gerçek** Metin2 client ve
sunucusuna bağlar. Hepsi derleme bayraklarının arkasındadır; normal `Metin2.exe` ve canlı sunucu
build'lerine hiçbir şey girmez.

```text
orchestrator (Python 3)  ──TCP 127.0.0.1:47800, JSON-lines──►  Metin2_QA.exe
                                                                   QaBridge.cpp  (soket, ana thread)
                                                                   qa_bridge.py  (JSON, durum, aksiyon)
                                                                   PythonQaModule.cpp (`qa` modülü)
                                                                        │ normal paketler
                                                                        ▼
                                                                 game/db/auth (QA sunucusu)
                                                                   cmd_qa.cpp   (/qa setup komutları)
                                                                   qa_event.cpp (qa_events.jsonl)
```

> Kod klasik kaynak ağacına (UserInterface / GameLib / EterPythonLib, `CHARACTER`, `cmd_gm.cpp`)
> göre yazıldı. Fork'unuzda fonksiyon adları farklıysa yorumlardaki "orijinal yol" notlarını
> takip ederek eşleştirin. Protokolün tamamı `qa/bridge/protocol.py` içinde; simülatör
> (`qa/sim/world.py`) her komut için referans davranıştır.

---

## 1. Client (Metin2_QA.exe)

### 1.1 Build bayrağı
Ayrı bir `QA` build konfigürasyonu oluşturun ve yalnızca onda tanımlayın:

```cpp
// UserInterface/Locale_inc.h (veya kullandığınız ortak define dosyası)
#define ENABLE_AI_QA_CLIENT
```

Python tarafı için `app` modülüne sabit ekleyin (UserInterface/PythonApplicationModule.cpp):

```cpp
#ifdef ENABLE_AI_QA_CLIENT
	PyModule_AddIntConstant(poModule, "ENABLE_AI_QA_CLIENT", 1);
#else
	PyModule_AddIntConstant(poModule, "ENABLE_AI_QA_CLIENT", 0);
#endif
```

### 1.2 Dosyalar
| Dosya | Nereye |
|---|---|
| `client/QaBridge.h`, `client/QaBridge.cpp` | `UserInterface/` (projeye ekleyin) |
| `client/PythonQaModule.cpp` | `UserInterface/` |
| `client/qa_bridge.py` | `root/` pack'i |
| Python 2.7 `json` (+ `base64`) paketi | istemcinin `lib/` klasörü (yoksa) |

### 1.3 C++ değişiklikleri

**Başlatma** — `UserInterface.cpp`, diğer `init*()` modül kayıtlarının yanına:

```cpp
#ifdef ENABLE_AI_QA_CLIENT
	initqa();
#endif
```

`CPythonApplication` (PythonApplication.h) üyesi olarak `CQaBridge m_qaBridge;` ekleyin ve
`CPythonApplication::Create` sonunda:

```cpp
#ifdef ENABLE_AI_QA_CLIENT
	// Çoklu ajan için her istemci ayrı port: Metin2_QA.exe --qa-port 47801
	unsigned short qaPort = 47800;
	if (const char* p = strstr(GetCommandLineA(), "--qa-port "))
		qaPort = (unsigned short)atoi(p + 10);
	m_qaBridge.Initialize(qaPort);
#endif
```

**Her frame** — `CPythonApplication::Process()` içinde, `m_pyNetworkStream.Process()`'ten sonra:

```cpp
#ifdef ENABLE_AI_QA_CLIENT
	CQaBridge::Instance().Process();
#endif
```

**Sistem mesajları** — `PythonChat.cpp`, `CPythonChat::AppendChat(int iType, const char* c_szChat)` başı:

```cpp
#ifdef ENABLE_AI_QA_CLIENT
	CQaBridge::Instance().OnChat(iType, c_szChat);
#endif
```

**Tıklama yardımcıları** — `PythonQaModule.cpp` sonundaki `#if 0` bloğunda yer alan
`CPythonPlayer::QA_MoveTo / QA_ClickActor / QA_ClickItem` ve `CPythonItem::QA_GetGroundItems`
gövdelerini ilgili .cpp dosyalarına, bildirimlerini de `PythonPlayer.h` / `PythonItem.h`
(public) içine `#ifdef ENABLE_AI_QA_CLIENT` ile ekleyin. Bu fonksiyonlar oyuncunun fare
tıklamasının kullandığı private `__OnClickActor` / `__OnClickItem` /
`NEW_MoveToDestPixelPositionDirection` yollarını çağırır — yani sunucuya giden paketler
gerçek oyuncununkiyle birebir aynıdır.

### 1.4 Python UI kancaları (root)
Her kanca `if app.ENABLE_AI_QA_CLIENT:` ile korunmalı.

```python
# introLogin.py — LoginWindow.__init__ sonu
if app.ENABLE_AI_QA_CLIENT:
	import qa_bridge
	qa_bridge.RegisterStage("login", self)

# introLogin.py — giriş hatası popup'ını gösteren fonksiyonda (ör. OnLoginFailure)
if app.ENABLE_AI_QA_CLIENT:
	import qa_bridge
	qa_bridge.OnLoginFailure(error)

# introSelect.py — SelectCharacterWindow.Open sonu (karakter listesi yüklendikten sonra)
if app.ENABLE_AI_QA_CLIENT:
	import qa_bridge
	qa_bridge.RegisterStage("select", self)

# game.py — GameWindow.Open sonu / Close başı
if app.ENABLE_AI_QA_CLIENT:
	import qa_bridge
	qa_bridge.OnEnterGame()     # Close'da: qa_bridge.OnLeaveGame()

# game.py — StartShop(self, vid) / EndShop(self)
if app.ENABLE_AI_QA_CLIENT:
	import qa_bridge
	qa_bridge.OnShopOpen(vid)   # EndShop'ta: qa_bridge.OnShopClose()
```

**Ticaret ve grup** — `game.py`:

```python
# StartExchange(self) / EndExchange(self)
if app.ENABLE_AI_QA_CLIENT:
	import qa_bridge
	qa_bridge.OnTradeStart()            # EndExchange'te: qa_bridge.OnTradeEnd()

# RecvPartyInviteQuestion(self, leaderVID, leaderName) — soru penceresini açmadan önce
if app.ENABLE_AI_QA_CLIENT:
	import qa_bridge
	qa_bridge.OnPartyInvite(leaderVID, leaderName)

# AddPartyMember(self, pid, name) / RemovePartyMember(self, pid) / ExitParty(self)
if app.ENABLE_AI_QA_CLIENT:
	import qa_bridge
	qa_bridge.OnPartyMember(pid, name, is_leader=<lider mi>)   # Remove: OnPartyMemberRemoved(pid), Exit: OnPartyExit()
```

Aksiyonlar UI'nin kullandığı paketlerle gider: `net.SendExchangeStartPacket`,
`SendExchangeItemAddPacket`, `SendExchangeElkAddPacket`, `SendExchangeAcceptPacket`,
`SendExchangeExitPacket`, `SendPartyInvitePacket`, `SendPartyInviteAnswerPacket`,
`SendPartyExitPacket`, `SendPartyRemovePacket`.

**Görev diyaloğu** — `uiQuest.py`, `QuestDialog` seçenek butonları oluşturulduktan sonra
(ör. `MakeQuestion` sonu). Metin ve seçenekleri toplayıp, seçimi normal buton tıklamasıyla
aynı yoldan yapan fonksiyonu verin:

```python
if app.ENABLE_AI_QA_CLIENT:
	import qa_bridge
	options = [btn.GetText() for btn in self.btnAnswer]          # seçenek metinleri
	def qa_select(i, self=self):
		event.SelectAnswer(self.descIndex, i)                     # butonun yaptığıyla aynı
		self.CloseSelf()
	qa_bridge.OnQuestDialog(self.qa_text if hasattr(self, "qa_text") else "", options,
	                        qa_select, self.CloseSelf)
```

`CloseSelf` / pencere kapanışında `qa_bridge.OnQuestDialogClosed()` çağırın.

### 1.5 Bilinen sınırlar (v1)
- `change_channel` QA client'ta henüz yok (`NOT_SUPPORTED` döner).
- `get_quest_state` klasik `quest` modülünün sayacı kadar bilgi verir; kesin görev durumu için
  sunucu `QA_EVENT("QUEST_PROGRESS", ...)` olaylarını ve `server_event` assertion'ını kullanın.
- `entity_dead` olayı, saldırılan hedefin ölmesi/kaybolmasından türetilir.
- Gerçek sunucuda dünya rastgeleliği (hasar, drop) seed ile kontrol edilemez; replay aynı
  aksiyon dizisini ve bekleme sürelerini birebir tekrarlar, dünya farkları "FLAKY" olarak raporlanır.

---

## 2. Sunucu (game)

### 2.1 Build bayrağı ve CONFIG
```cpp
// service.h / CommonDefines.h
#define ENABLE_AI_QA_SERVER
```

`config.cpp` içinde (diğer TOKEN'ların yanına):
```cpp
#ifdef ENABLE_AI_QA_SERVER
		TOKEN("qa_mode")
		{
			str_to_number(g_bQaMode, value_string);
			if (g_bQaMode)
				fprintf(stderr, "QA MODE ENABLED — production'da kullanmayın!\n");
		}
#endif
```
QA kanallarının `CONFIG` dosyasına `QA_MODE: 1` ekleyin. Canlı sunucu CONFIG'inde bu satır olmamalı.

### 2.2 Dosyalar
`server/qa_event.h`, `server/qa_event.cpp`, `server/cmd_qa.cpp` → `game/src/` ve
Makefile/CMakeLists kaynak listesine ekleyin. `cmd.cpp` kayıt satırları `cmd_qa.cpp` başındadır.

### 2.3 `IsQaBot()`
`char.h` (public):
```cpp
#ifdef ENABLE_AI_QA_SERVER
	bool IsQaBot() const { return g_bQaMode && IsPC() && !strncmp(GetName(), "AI_QA_", 6); }
#endif
```
`IsQaBot()` **oyun avantajı vermek için değil**, yalnızca `/qa` hazırlık komutlarına izin ve
loglama için kullanılır.

### 2.4 Olay noktaları (QA_EVENT / QA_ASSERT)
Test ettiğiniz sistemlerin kritik noktalarına ekleyin. Başlangıç için önerilenler:

| Yer | Olay |
|---|---|
| `char_battle.cpp` `CHARACTER::Dead` | `QA_EVENT("MOB_KILL", pkKiller, QA_KV("vnum", GetRaceNum()), QA_KV("vid", (DWORD)GetVID()))` / oyuncu ise `PLAYER_DEAD` |
| `char_item.cpp` `UseItemEx` | `QA_EVENT("ITEM_USE", this, QA_KV("vnum", item->GetVnum()))` |
| `char_item.cpp` `EquipItem` | `QA_EVENT("EQUIP", this, QA_KV("vnum", item->GetVnum()), QA_KV("cell", iWearCell))` |
| `char_item.cpp` `PickupItem` | `QA_EVENT("ITEM_PICKUP", this, QA_KV("vnum", ...), QA_KV("count", ...))` |
| `shop.cpp` `CShop::Buy` (başarılı) | `QA_EVENT("SHOP_BUY", ch, QA_KV("vnum", ...), QA_KV("price", ...))` |
| `input_main.cpp` shop sell | `QA_EVENT("SHOP_SELL", ch, ...)` |
| `char.cpp` `PointChange(POINT_GOLD)` sonrası | `QA_ASSERT(GetGold() >= 0, "PLAYER_NEGATIVE_GOLD", this, QA_KV("gold", GetGold()))` |
| `exchange.cpp` `CExchange::Done` (başarılı) | `QA_EVENT("TRADE_COMPLETE", ch, QA_KV("partner", ...))` |
| `exchange.cpp` iptal / `CheckSpace` başarısız | `QA_EVENT("TRADE_CANCEL", ch, QA_KV("reason", "INVENTORY_FULL"))` |
| `party.cpp` Join / Quit / Destroy | `PARTY_JOIN`, `PARTY_LEAVE`, `PARTY_DISBAND` |
| `char_battle.cpp` party exp dağıtımı | `QA_EVENT("PARTY_EXP", ch, QA_KV("total", ...), QA_KV("share", ...))` |
| `questlua_*.cpp` hata dalları | `QA_ERROR("QUEST_ERROR", ch, QA_KV("quest", ...))` |
| Yeni geliştirilen her sistem | durum geçişlerinde `QA_EVENT`, değişmezlerde `QA_ASSERT` |

İsimler senaryolardaki `server_event: {name: ...}` assertion'larıyla eşleşir. SYSERR'ler ayrıca
`qa.toml [server.log_files]` üzerinden (syserr dosyası) yakalanır; her run'da örtük olarak
`server_errors == 0` ve `qa_asserts == 0` beklenir.

### 2.5 Test hesapları
`sql/qa_accounts.sql` yalnızca QA veritabanında çalıştırılır. Karakterleri ilk seferde QA
client ile normal karakter oluşturma ekranından, hesap adıyla aynı isimde oluşturun.

---

## 3. Orchestrator'ı gerçek client'a bağlama

`qa.local.toml`:
```toml
env = "qa"
source_repo = "C:/m2/src"             # rapordaki commit bilgisi

[bridge]
mode = "tcp"
host = "127.0.0.1"                    # orchestrator client'la aynı makinede
port = 47800

[server]
events_file = "//qa-server/metin2/channel1/core1/qa_events.jsonl"   # veya SSHFS/SMB bağlantısı

[server.log_files]
syserr = "//qa-server/metin2/channel1/core1/syserr"

[build.commands]
server = ["ssh", "qa-server", "gmake -C /usr/metin2/src/game/src -j8"]
client = ["msbuild", "C:/m2/client/Metin2Client.sln", "/p:Configuration=QA", "/m"]
```

**Çoklu ajan** (trade/party senaryoları): her ajan ayrı bir `Metin2_QA.exe` örneğidir ve ayrı
bridge portu dinler (ör. komut satırı `--qa-port 47801`). Portları eşleyin:

```toml
[bridge.agent_ports]
AI_QA_001 = 47800
AI_QA_002 = 47801
```

Tanımlı değilse `port + ajan sırası` kullanılır.

Doğrulama sırası:
1. `Metin2_QA.exe`'yi açın, login ekranında bekletin.
2. `metin2-qa run smoke_login_walk` → login, karakter seçimi, yürüme, screenshot.
3. `metin2-qa run shop_insufficient_gold` → `/qa` komutları, NPC dükkanı, sistem mesajları.
4. Sonra kendi senaryolarınız.

Senaryolardaki koordinat/vnum değerleri simülatör haritasına göredir; gerçek harita için
(ör. map1 köy merkezi, gerçek NPC/mob vnum'ları) `scenarios/` altında kopyalarını oluşturun.
