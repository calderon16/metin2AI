@echo off
rem 7/24 QA servisi (Görev Zamanlayıcı bunu çalıştırır). Şifreler kullanıcı ortam değişkenlerinden gelir
rem (QA_ACCOUNT_PASSWORD, isteğe bağlı QA_PANEL_TOKEN / GEMINI_API_KEY); bu dosyada şifre yok.
cd /d "%~dp0..\.."
if not exist artifacts mkdir artifacts
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m qa --config qa.local.toml daemon >> artifacts\daemon.log 2>&1
