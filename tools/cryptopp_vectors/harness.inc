// Appended to a /tmp copy of the server's cipher.cpp (same translation unit) to emit reference vectors.
#include <cstdio>
#include <cryptopp/mars.h>
#include <cryptopp/cast.h>
#include <cryptopp/twofish.h>

static void hex(FILE* f, const byte* p, size_t n) {
  for (size_t i = 0; i < n; ++i) fprintf(f, "%02x", p[i]);
}

// Table symbols straight from libcryptopp.a (nested Base classes are private)
extern const word32 g_mars_sbox[512] __asm__("_ZN8CryptoPP4MARS4Base4SboxE");
extern const word32 g_cast_s[8][256] __asm__("_ZN8CryptoPP4CAST1SE");
extern const byte g_twofish_q[2][256] __asm__("_ZN8CryptoPP7Twofish4Base1qE");
extern const word32 g_twofish_mds[4][256] __asm__("_ZN8CryptoPP7Twofish4Base3mdsE");

int main() {
  // 1) tables
  FILE* t = fopen("tables.txt", "w");
  fprintf(t, "mars_sbox ");
  for (int i = 0; i < 512; ++i) fprintf(t, "%08x", g_mars_sbox[i]);
  fprintf(t, "\n");
  for (int k = 0; k < 4; ++k) {
    fprintf(t, "cast_s%d ", k + 1);
    for (int i = 0; i < 256; ++i) fprintf(t, "%08x", g_cast_s[k][i]);
    fprintf(t, "\n");
  }
  for (int k = 0; k < 2; ++k) {
    fprintf(t, "twofish_q%d ", k);
    hex(t, g_twofish_q[k], 256);
    fprintf(t, "\n");
  }
  for (int k = 0; k < 4; ++k) {
    fprintf(t, "twofish_mds%d ", k);
    for (int i = 0; i < 256; ++i) fprintf(t, "%08x", g_twofish_mds[k][i]);
    fprintf(t, "\n");
  }
  fclose(t);

  // 2) per-algorithm CTR keystream (3 blocks) = E(iv), E(iv+1), E(iv+2)
  FILE* a = fopen("algorithms.txt", "w");
  for (int sel = 0; sel < BlockCipherAlgorithm::kMaxAlgorithms; ++sel) {
    std::unique_ptr<BlockCipherAlgorithm> alg(BlockCipherAlgorithm::Pick(sel));
    size_t kl = alg->GetDefaultKeyLength(), bs = alg->GetBlockSize();
    SecByteBlock key(kl), iv(bs), buf(bs * 3 + 5);
    for (size_t i = 0; i < kl; ++i) key[i] = (byte)(i * 7 + 1);
    for (size_t i = 0; i < bs; ++i) iv[i] = (byte)(0xa0 + i);
    // last byte near overflow to exercise the counter carry
    iv[bs - 1] = 0xff;
    memset(buf, 0, buf.size());
    std::unique_ptr<SymmetricCipher> enc(alg->CreateEncoder(key, kl, iv));
    enc->ProcessData(buf, buf, 4);
    enc->ProcessData(buf + 4, buf + 4, buf.size() - 4);
    fprintf(a, "%d %u %u ", sel, (unsigned)kl, (unsigned)bs);
    hex(a, key, kl); fprintf(a, " ");
    hex(a, iv, bs); fprintf(a, " ");
    hex(a, buf, buf.size()); fprintf(a, "\n");
  }
  fclose(a);

  // 3) full key agreement + both directions, until every algorithm is seen in both slots
  FILE* h = fopen("handshakes.txt", "w");
  bool seen0[32] = {false}, seen1[32] = {false};
  int wrote = 0;
  for (int round = 0; round < 4000 && wrote < 64; ++round) {
    Cipher server, client;
    byte sdata[256], cdata[256];
    size_t slen = sizeof(sdata), clen = sizeof(cdata);
    size_t agreed = server.Prepare(sdata, &slen);
    size_t agreed_c = client.Prepare(cdata, &clen);
    if (!agreed || agreed != agreed_c) { fprintf(stderr, "prepare failed\n"); return 1; }
    DH2KeyAgreement* ck = (DH2KeyAgreement*)client.key_agreement_;
    SecByteBlock cs(ck->spriv_key_), ce(ck->epriv_key_);
    if (!client.Activate(true, agreed, sdata, slen)) { fprintf(stderr, "client activate failed\n"); return 1; }
    // recompute shared for the record (same as client's)
    DH2KeyAgreement* sk = (DH2KeyAgreement*)server.key_agreement_;
    SecByteBlock shared(sk->dh2_.AgreedValueLength());
    if (!sk->dh2_.Agree(shared, sk->spriv_key_, sk->epriv_key_, cdata, cdata + sk->dh2_.StaticPublicKeyLength())) {
      fprintf(stderr, "agree failed\n"); return 1;
    }
    if (!server.Activate(false, agreed, cdata, clen)) { fprintf(stderr, "server activate failed\n"); return 1; }
    int s0 = shared[shared[0] % shared.size()] % BlockCipherAlgorithm::kMaxAlgorithms;
    int s1 = shared[shared[1] % shared.size()] % BlockCipherAlgorithm::kMaxAlgorithms;
    if (seen0[s0] && seen1[s1] && round > 3) continue;
    seen0[s0] = seen1[s1] = true;
    byte m1[70], m2[70];
    for (int i = 0; i < 70; ++i) { m1[i] = (byte)(i * 3 + 1); m2[i] = (byte)(255 - i * 5); }
    server.Encrypt(m1, 5); server.Encrypt(m1 + 5, 65);       // server -> client
    client.Encrypt(m2, 33); client.Encrypt(m2 + 33, 37);     // client -> server
    fprintf(h, "%d %d %u ", s0, s1, (unsigned)agreed);
    hex(h, sdata, slen); fprintf(h, " ");
    hex(h, cdata, clen); fprintf(h, " ");
    hex(h, cs, cs.size()); fprintf(h, " ");
    hex(h, ce, ce.size()); fprintf(h, " ");
    hex(h, shared, shared.size()); fprintf(h, " ");
    hex(h, m1, 70); fprintf(h, " ");
    hex(h, m2, 70); fprintf(h, "\n");
    ++wrote;
    bool all = true;
    for (int i = 0; i < BlockCipherAlgorithm::kMaxAlgorithms; ++i) all = all && seen0[i] && seen1[i];
    if (all) break;
  }
  fclose(h);
  printf("ok wrote=%d\n", wrote);
  return 0;
}
