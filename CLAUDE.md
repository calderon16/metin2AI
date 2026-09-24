# Metin2 AI QA Player — Claude için notlar

- Kullanıcıyla her zaman **Türkçe** yaz. Kod içi yorumlar ve dokümanlar da Türkçe.
- Python 3.11+, bağımlılıklar `.venv` içinde: `.venv/bin/pip install -e ".[dev]"`.
- Testler: `.venv/bin/python -m pytest -q` (simülatöre karşı, ~2 sn). Her değişiklikten sonra çalıştır.
- `integration/client/qa_bridge.py` **Python 2.7** uyumlu kalmalı (f-string yok, `except X, e` biçimi).
- Bridge protokolünü değiştirirsen üç yeri birlikte güncelle: `qa/bridge/protocol.py`,
  `qa/sim/world.py` (SimClient.cmd_*), `integration/client/qa_bridge.py`.
  `tests/test_sim_bridge.py::test_every_sim_command_is_in_protocol` bunu kontrol eder.
- Yeni behaviour: `qa/engine/behaviours.py` içinde `@behaviour("ad")`; yeni assertion:
  `qa/oracle/assertions.py` içinde `@assertion("ad")`. Şema ve MCP referansı otomatik güncellenir.
- Test adımları yalnızca oyuncu aksiyonlarını kullanır; `/qa` komutları sadece senaryonun `setup` bölümünde.
- Yeni senaryoya test ettiği kaynak dosyaları `covers:` ile yaz; `run_affected` seçimi buna dayanır.
- Çoklu ajan senaryolarında (trade, party, pazar) her zaman `conservation` assertion'ı ekle.
- Senaryolar seed'e bağlı kararsız olmamalı: yeni senaryoyu birkaç farklı seed ile çalıştırıp doğrula.

## QA döngüsü (MCP `metin2-qa`)
Metin2 kaynağında değişiklik yaptıktan sonra: `build` → `run_affected` (değişen dosyalara göre) →
FAILED ise `get_test_result`, `get_trace`, `get_server_logs`, `get_screenshot` ile kök nedeni bul →
düzelt → tekrar çalıştır → `replay_failure(eski_run)` ile düzeltmeyi doğrula. Yeni bir sistem
geliştirdiğinde onun senaryosunu da `write_scenario` ile ekle.
