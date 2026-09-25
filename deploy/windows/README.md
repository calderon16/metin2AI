# Windows'ta 7/24 çalıştırma

Servis saf Python olduğundan Windows'ta da çalışır.

```bat
cd C:\metin2AI
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .
setx QA_PANEL_TOKEN "degistir"
setx QA_ACCOUNT_PASSWORD "degistir"
.venv\Scripts\python -m qa --config qa.local.toml daemon
```

Oturum kapansa da çalışması için iki yol:

**NSSM (önerilen)** — https://nssm.cc
```bat
nssm install Metin2QA C:\metin2AI\.venv\Scripts\python.exe -m qa --config C:\metin2AI\qa.local.toml daemon
nssm set Metin2QA AppDirectory C:\metin2AI
nssm set Metin2QA AppEnvironmentExtra QA_PANEL_TOKEN=degistir QA_ACCOUNT_PASSWORD=degistir
nssm set Metin2QA AppStdout C:\metin2AI\artifacts\daemon.log
nssm start Metin2QA
```

**Görev Zamanlayıcı** — "Bilgisayar başladığında" tetikleyicisiyle, "kullanıcı oturum açmış olsun ya da
olmasın çalıştır" seçeneğiyle aynı komutu çalıştıran bir görev oluşturun.
