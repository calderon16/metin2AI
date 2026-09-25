"""Geliştirilmiş paket şifrelemesi (_IMPROVED_PACKET_ENCRYPTION_), SEQUENCE baytı ve profil ayrıştırıcı eklemeleri.

Referans vektörler sunucunun kendi Crypto++ kitaplığıyla üretildi (tests/data/README.md): her algoritma için
CTR anahtar akışı ve her algoritmayı iki yönde de kapsayan tam DH2 anlaşmaları.
"""

from pathlib import Path

import pytest

from qa.bridge.client import LocalBridge
from qa.config import HeadlessConfig
from qa.headless.blockciphers import ALGORITHMS, CtrStream, pick
from qa.headless.client import HeadlessClient
from qa.headless.crypto import (DH_G, DH_P, ImprovedCrypto, ImprovedKeyAgreement, KeyAgreementError,
                                make_crypto)
from qa.headless.fakeserver import FakeMetin2
from qa.headless.profile import build_profile, import_profile, parse_sequence_table

D = Path(__file__).parent / "data"
F = Path(__file__).parent / "fixtures" / "headless"


def _rows(name):
    return [line.split() for line in (D / name).read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------- blok şifreler

@pytest.mark.parametrize("row", _rows("cryptopp_algorithms.txt"), ids=lambda r: f"sel{r[0]}")
def test_block_cipher_ctr_matches_cryptopp(row):
    sel, kl, bs, key, iv, stream = int(row[0]), int(row[1]), int(row[2]), *map(bytes.fromhex, row[3:])
    cipher = pick(sel)(key)
    assert (cipher.key_size, cipher.block_size) == (kl, bs)
    ctr = CtrStream(cipher, iv)
    # parça parça işlense de anahtar akışı sürer; IV'nin son baytı 0xff → sayaç taşması da sınanır
    got = ctr.process(bytes(4)) + ctr.process(bytes(len(stream) - 4))
    assert got == stream


def test_algorithm_table_matches_server_enum():
    names = [a.name for a in ALGORITHMS]
    assert len(names) == 14 and names[0] == names[3] == "twofish"   # kDefault → Twofish
    assert pick(14 + 5) is ALGORITHMS[5]


# ---------------------------------------------------------------------- DH2 + yön seçimi

@pytest.mark.parametrize("row", _rows("cryptopp_handshakes.txt"), ids=lambda r: f"alg{r[0]}-{r[1]}")
def test_key_agreement_matches_cryptopp(row):
    agreed = int(row[2])
    sdata, cdata, cs, ce, shared, ct1, ct2 = map(bytes.fromhex, row[3:])
    ka = ImprovedKeyAgreement(int.from_bytes(cs, "big"), int.from_bytes(ce, "big"))
    assert ka.data == cdata                                   # açık anahtarlar Crypto++ ile aynı kodlanır
    assert ka.shared(agreed, sdata) == shared
    c = ImprovedCrypto(shared, polarity=True)
    m1 = bytes((i * 3 + 1) & 255 for i in range(70))           # sunucu → istemci
    m2 = bytes((255 - i * 5) & 255 for i in range(70))         # istemci → sunucu
    assert c.decrypt(ct1[:9]) + c.decrypt(ct1[9:]) == m1
    assert c.encrypt(m2[:1]) + c.encrypt(m2[1:]) == ct2
    # sunucu kutbu da aynı akışı üretir
    s = ImprovedCrypto(shared, polarity=False)
    assert s.encrypt(m1) == ct1 and s.decrypt(ct2) == m2


def test_key_agreement_rejects_bad_input():
    ka = ImprovedKeyAgreement()
    peer = ImprovedKeyAgreement().data
    with pytest.raises(KeyAgreementError, match="uzunluğu"):
        ka.shared(128, peer)
    with pytest.raises(KeyAgreementError, match="uzunluğu"):
        ka.shared(256, peer[:200])
    with pytest.raises(KeyAgreementError, match="grubunda"):
        ka.shared(256, (1).to_bytes(128, "big") + peer[128:])
    assert pow(DH_G, ka.static_private, DH_P) == ka.static_public


def test_make_crypto_names():
    assert make_crypto("improved").name == "none"   # anlaşmaya kadar düz
    with pytest.raises(ValueError, match="improved"):
        make_crypto("aes")


# ---------------------------------------------------------------------- profil ayrıştırıcı

def test_profile_parser_handles_real_world_structs():
    p = import_profile([F / "packet_sample.h", F / "packet_improved.h"], [F / "PythonNetworkStream.cpp", F / "packet_info.cpp"],
                       [], [], "fx", sequence_table=F / "sequence.cpp")
    assert p.sizeof("TPacketKeyAgreement") == 261               # struct içi static const dizi boyutu
    assert p.packets["HEADER_GC_KEY_AGREEMENT"]["struct"] == "TPacketKeyAgreement"
    assert p.sizeof("TFixturePos") == 3                          # metot gövdeleri atlandı
    assert p.sizeof("TFixtureMixed") == 4 + 3 + 5                # adlı enum alanı int, iç enum sabiti, *PTR takma adı
    assert p.sequence[:3] == [0xAF, 0xCA, 0x8A] and len(p.sequence) == 16
    assert p.packets["HEADER_CG_MOVE"].get("sequence") and not p.packets["HEADER_CG_ON_CLICK"].get("sequence")


def test_size_table_maps_server_names_by_value():
    client_h = ("enum { HEADER_CG_CHARACTER_MOVE = 7, HEADER_CG_PONG = 0xfe };\n"
                "typedef struct c { BYTE bHeader; long lX; } TPacketCGMove;")
    server_h = (F / "server_names.h").read_text(encoding="utf-8")
    table = ('Set(HEADER_CG_PLAYER_WALK, sizeof(TPacketCGMove), "Move", true);\n'
             'Set(HEADER_CG_BYTE_ONLY, sizeof(BYTE), "Pong", true);')
    p = build_profile([client_h], [table], {}, value_header_texts=[server_h])
    assert p.packets["HEADER_CG_CHARACTER_MOVE"] == {"header": 7, "struct": "TPacketCGMove", "dynamic": False,
                                                     "sequence": True}
    pong = p.packets["HEADER_CG_PONG"]
    assert pong["sequence"] and p.sizeof(pong["struct"]) == 1   # sizeof(BYTE) → tek alanlı paket


def test_sequence_table_parse():
    assert parse_sequence_table("const BYTE gc_abSequence[N] = { 0x1, 2, /* x */ 0xff };") == [1, 2, 255]


# ---------------------------------------------------------------------- uçtan uca (sahte sunucu)

@pytest.fixture(scope="module")
def improved_prof():
    return import_profile([F / "packet_sample.h", F / "packet_improved.h"],
                          [F / "PythonNetworkStream.cpp", F / "packet_info.cpp"], [], [], "fx-improved",
                          sequence_table=F / "sequence.cpp")


def _cfg(server, **kw):
    return HeadlessConfig(auth_port=server.auth_port, channels={"1": server.game_port}, timeout_s=5,
                          move_interval_ms=100, walk_speed=1500, **kw)


def test_headless_over_improved_encryption_and_sequence(improved_prof):
    server = FakeMetin2(improved_prof, improved=True)
    try:
        c = HeadlessClient(_cfg(server, crypto="improved"), profile=improved_prof)
        b = LocalBridge(c.handle, on_close=c.close)
        b.connect()
        b.call("login", account="AI_QA_001", password="qa")
        b.call("select_character", name="AI_QA_001")
        b.call("send_chat", message="/qa gold 750")
        b.call("move_to", x=5600, y=5000)
        b.call("wait", ms=900)
        st = b.call("get_player_state")
        assert st["in_game"] and st["gold"] == 750 and st["x"] == 5600
        stats = server.world.stats
        assert stats["key_agreements"] == 2                       # auth + game bağlantısı
        assert stats["seq_packets"] >= 5 and stats["seq_errors"] == 0
        assert stats["pongs"] == 1
        log = " ".join(line["text"] for line in b.call("get_client_log"))
        assert "şifreleme etkin" in log and "anahtar anlaşması" in log
        b.close()
    finally:
        server.shutdown()


def test_headless_follows_warp_to_another_core(improved_prof):
    server = FakeMetin2(improved_prof, improved=True)
    try:
        c = HeadlessClient(_cfg(server, crypto="improved"), profile=improved_prof)
        b = LocalBridge(c.handle, on_close=c.close)
        b.connect()
        b.call("login", account="AI_QA_001", password="qa")
        b.call("select_character", name="AI_QA_001")
        b.drain_events()
        b.call("send_chat", message="/qa warp")
        b.call("wait", ms=1500)
        st = b.call("get_player_state")
        assert st["in_game"] and st["name"] == "AI_QA_001"
        assert server.world.stats["logins"] == 2 and server.world.stats["key_agreements"] == 3
        assert server.world.stats["seq_errors"] == 0
        assert "map_loaded" in [e["event"] for e in b.drain_events()]
        assert any("warp tamam" in line["text"] for line in b.call("get_client_log"))
        b.close()
    finally:
        server.shutdown()


def test_headless_refuses_improved_server_without_config(improved_prof):
    server = FakeMetin2(improved_prof, improved=True)
    try:
        c = HeadlessClient(_cfg(server), profile=improved_prof)
        with pytest.raises(Exception, match="improved"):
            LocalBridge(c.handle).call("login", account="AI_QA_001", password="qa")
    finally:
        server.shutdown()
