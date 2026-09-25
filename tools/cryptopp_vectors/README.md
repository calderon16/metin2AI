# Crypto++ referans vektörleri

`_IMPROVED_PACKET_ENCRYPTION_` uygulamasının (qa/headless/crypto.py, blockciphers.py) doğruluğu, sunucunun
**kendi** Crypto++ kitaplığı ve **kendi** `cipher.cpp`'siyle üretilen vektörlerle sınanır.

Üretici sunucu makinesinde (FreeBSD, oyun kaynağının derlendiği yer) çalışır ve hiçbir şeyi değiştirmez:
`/tmp/qavec` altında `cipher.cpp`'nin bir **kopyasını** alır, özel üyeleri okunabilir yapar, rastgele üreteci
tohumlu `LC_RNG` ile değiştirir ve sonuna `harness.inc`'i ekleyip derler.

```sh
mkdir -p /tmp/qavec && cp harness.inc run.sh /tmp/qavec/ && sh /tmp/qavec/run.sh
```

`run.sh` içindeki `SRC` (oyun kaynağı) ve `/usr/metin2/src/extern` (Crypto++ başlıkları + libcryptopp.a) yollarını
kendi kurulumunuza göre değiştirin. Çıktılar:

| Dosya | İçerik | Depodaki yeri |
|---|---|---|
| `algorithms.txt` | 14 algoritmanın her biri için anahtar, IV (son bayt 0xff → sayaç taşması) ve CTR anahtar akışı | `tests/data/cryptopp_algorithms.txt` |
| `handshakes.txt` | Her algoritmayı iki yönde de kapsayan tam DH2 anlaşmaları: sunucu/istemci açık anahtarları, istemcinin gizli anahtarları, ortak sır, iki yönde şifreli örnek | `tests/data/cryptopp_handshakes.txt` |
| `tables.txt` | MARS S-kutusu, CAST S1–S4, Twofish q/MDS (libcryptopp.a'dan) | `gen_tables.py` ile `qa/headless/_cipher_tables.py` |

```sh
python tools/cryptopp_vectors/gen_tables.py tables.txt qa/headless/_cipher_tables.py
```

Fork'unuzun `cipher.cpp`'si farklıysa (başka algoritma listesi, farklı anahtar/IV seçimi) vektörleri yeniden üretin;
`tests/test_headless_crypto.py` farkı hemen gösterir.
