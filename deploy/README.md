# Sunucuya (VM) kurulum

AI QA servisi test sunucusunun VM'inde, Metin2 auth/game süreçlerinin yanında çalışır ve web panelini sunar.

1. Kodu VM'e alın ve kurun:
   ```sh
   git clone https://github.com/calderon16/metin2AI && cd metin2AI
   python3.11 -m venv .venv && .venv/bin/pip install -e .
   cp qa.toml qa.local.toml        # yerel ayarlar (git'e girmez)
   ```
2. `qa.local.toml`'u düzenleyin:
   - `[bridge] mode = "headless"` ve `[headless]` auth/kanal portları, paket profili
     (`metin2-qa packets import ...` ile kaynaktan üretilir — bkz. integration/INTEGRATION.md)
   - `[[daemon.agents]]` AI_QA_* hesapları, `[[daemon.schedules]]` (ör. her gece kampanya)
   - `[server.log_files]` syserr yolları, `[server] events_file` (qa_events.jsonl)
3. Şifreleri ortam değişkeni olarak verin: `QA_PANEL_TOKEN`, `QA_ACCOUNT_PASSWORD`, isteğe bağlı `GEMINI_API_KEY`.
4. Servisi kurun: FreeBSD → `freebsd/metin2qa` (rc.d), Linux → `systemd/metin2-qa.service`,
   Windows → `windows/README.md`.
5. Panel: `http://127.0.0.1:8765`. VM dışından erişim için ya SSH tüneli
   (`ssh -L 8765:127.0.0.1:8765 vm`) ya da `[daemon] host = "0.0.0.0"` (bu durumda QA_PANEL_TOKEN zorunlu;
   mümkünse yalnızca iç ağa/VPN'e açın).
6. Claude'u aynı ajanlara bağlamak için MCP ortamına `QA_DAEMON_URL=http://127.0.0.1:8765` ve `QA_PANEL_TOKEN` ekleyin.
