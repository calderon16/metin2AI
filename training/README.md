# Metin2Re QA modeli: kendini geliştiren döngü

AI oyuncular (AI_QA_001..004) 7/24 oyunda test yaparken kararlarını **yerel bir Ollama modeli** verir
(kota ve ücret yok). Oyuncuların her keşfi kayıt altına alınır. Bu kayıtlar haftalık olarak eğitim verisine
dönüşür, model ücretsiz GPU'da (Kaggle) ince ayarlanır ve yeni model eskisinden **ölçülebilir biçimde iyiyse**
devreye alınır.

```
 oyna (explore_rotation, her 2 saat)  ──►  kayıtlar (llm_transcript.jsonl)
        ▲                                        │
        │                                        ▼
 metin2re-qa:latest  ◄── devreye al ◄── değerlendir ◄── Ollama'ya ekle ◄── eğit (Kaggle) ◄── veri (export)
                        (daha iyiyse)   (eval_goals)                       learning_cycle (haftalık)
```

| Dosya | Görevi |
|---|---|
| `collect_goals.yaml` | Veri toplama hedefleri (servis sırayla koşar) |
| `eval_goals.yaml` | Sabit sınav seti: modeller hep bununla karşılaştırılır |
| `evaluate.py` | Modelleri sınav setinde gerçek sunucuda ölçer (skor = hedef + geçerli komut + bitirme) |
| `export_dataset.py` | Kayıtlardan eğitim verisi (train/val.jsonl); hatalı komutlar öğretilmez, gerçek oyuncu adları gizlenir |
| `notebooks/metin2re_qlora.ipynb` | Kaggle/Colab eğitim not defteri (Unsloth QLoRA → GGUF) |
| `kaggle_train.py` | Kaggle'da eğitimi otomatik çalıştırır (hesap bağlıysa) |
| `import_model.py` | GGUF'u Ollama'ya ekler (temel modelin tool şablonuyla) |
| `promote.py` | Aday modeli mevcut modelle karşılaştırıp daha iyiyse devreye alır |
| `cycle.py` | Hepsini sırayla yapan döngü (servisin `learning_cycle` işi) |

## Kurulum (bir kez)

1. Ollama kurulu ve çalışıyor olmalı (`ollama list`).
2. `qa.local.toml`:
   ```toml
   [explorer]
   provider = "ollama"
   model = "metin2re-qa:latest"     # başlangıçta en iyi temel modelin kopyası
   num_ctx = 12288
   ```
3. Zamanlanmış işler (`qa.local.toml`):
   ```toml
   [[daemon.schedules]]
   name = "veri-toplama"
   every = "2h"
   job = { type = "explore_rotation" }

   [[daemon.schedules]]
   name = "haftalik-egitim"
   every = "168h"
   job = { type = "learning_cycle", base_ollama = "qwen3:8b", base_unsloth = "unsloth/Qwen3-8B-bnb-4bit" }
   ```
Bunlar Studio → AI QA Player → İşler'den elle de başlatılabilir.

## Eğitim hesabı

Eğitim için ücretsiz GPU gerekir. **Kaggle önerilir**: haftada ~30 saat ücretsiz GPU verir ve eğitim
otomatik çalışabilir. Colab da olur, ama orada adımları elle yaparsınız.

### A) Kaggle (önerilen, tam otomatik)

1. **Hesap:** https://www.kaggle.com → *Register* → Google hesabı ya da e-posta ile kaydolun.
2. **Telefon doğrulaması (zorunlu):** sağ üstte profil → *Settings* → *Phone Verification*. Doğrulama
   olmadan not defterlerinde GPU ve internet açılmaz.
3. **API anahtarı:** *Settings* → *API* → **Create New Token**. Tarayıcı `kaggle.json` dosyasını indirir.
4. **Anahtarı yerleştirin:** dosyayı `C:\Users\<kullanıcı>\.kaggle\kaggle.json` konumuna taşıyın
   (`.kaggle` klasörü yoksa oluşturun). **Bu dosyayı depoya, e-postaya ya da sohbete koymayın.**
   Alternatif: `KAGGLE_USERNAME` ve `KAGGLE_KEY` kullanıcı ortam değişkenleri.
5. **Deneme:** `metin2AI\.venv\Scripts\kaggle.exe datasets list -s metin2` bir liste döndürmeli.
6. Hazır. `learning_cycle` işi yeterli veri (150+ örnek) birikince veri kümesini **özel** olarak yükler,
   not defterini GPU'lu **özel** bir not defteri olarak çalıştırır (~20–40 dk), çıkan modeli indirir,
   Ollama'ya ekler, sınav setinde ölçer ve daha iyiyse devreye alır.
   İlerleme: kaggle.com → *Code* → `metin2re-qa-train`.

### B) Google Colab (elle)

1. https://colab.research.google.com → Google hesabıyla girin.
2. *Dosya* → *Not defteri yükle* → `training/notebooks/metin2re_qlora.ipynb`.
3. *Çalışma zamanı* → *Çalışma zamanı türünü değiştir* → **T4 GPU**.
4. Veri: `python training/export_dataset.py --out training/data/elle` → çıkan `train.jsonl`,
   `val.jsonl`, `manifest.json` dosyalarını not defteri sorduğunda yükleyin.
5. *Çalışma zamanı* → *Tümünü çalıştır*. Sonunda `metin2re-qa.Q4_K_M.gguf` iner (~4.7 GB).
6. Dosyayı `metin2AI/training/incoming/` klasörüne koyun. Bir sonraki `learning_cycle` onu alır,
   ya da hemen: `python training/cycle.py --gguf training/incoming/metin2re-qa.Q4_K_M.gguf`.

## Elle komutlar

```bat
:: modelleri sınav setinde karşılaştır (servis kapalıyken; açıksa İşler'den learning_cycle)
.venv\Scripts\python training\evaluate.py --model qwen2.5:7b --model metin2re-qa:latest
:: veri
.venv\Scripts\python training\export_dataset.py --out training\data\elle
:: GGUF'u ekle ve aday olarak dene
.venv\Scripts\python training\import_model.py training\incoming\metin2re-qa.Q4_K_M.gguf --tag metin2re-qa:aday
.venv\Scripts\python training\promote.py --candidate metin2re-qa:aday
```

## Güvenlik ve gizlilik

- Eğitime yalnızca AI_QA_ hesaplarının keşif kayıtları girer; gerçek oyuncuların adları `Oyuncu#n` ile
  değiştirilir, başkalarının sohbet satırları atılır.
- Kaggle veri kümesi ve not defteri **özel** (private) oluşturulur.
- Model yalnızca QA komutlarını (yürü, konuş, kes, ticaret…) kullanabilir; `/qa` hazırlık komutları
  yalnızca test sunucusunda (QA_MODE) ve AI_QA_ karakterlerinde çalışır.
- Model oyunun kendisine gömülmez: Ollama ayrı bir servis olarak çalışır, oyuna yük bindirmez.
