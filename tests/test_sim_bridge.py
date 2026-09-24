from qa.bridge.client import LocalBridge, TcpBridge
from qa.bridge.protocol import ActionError, ALL_COMMANDS
from qa.sim.server import serve_in_thread
from qa.sim.world import SimClient, SimWorld

import pytest


def _login(b):
    b.connect()
    b.call("login", account="AI_QA_001", password="qa")
    b.call("select_character", name="AI_QA_001")


def test_tcp_roundtrip_and_events():
    srv = serve_in_thread(port=0, world=SimWorld(seed=3))
    try:
        with TcpBridge("127.0.0.1", srv.server_address[1], timeout_s=5) as b:
            _login(b)
            assert b.info["client"] == "metin2_qa_sim"
            assert any(e["event"] == "map_loaded" for e in b.drain_events())
            st = b.call("get_player_state")
            assert st["in_game"] and st["name"] == "AI_QA_001"
            with pytest.raises(ActionError) as ei:
                b.call("no_such_command")
            assert ei.value.code == "UNKNOWN_COMMAND"
    finally:
        srv.shutdown()
        srv.server_close()


def test_wrong_password_rejected():
    b = LocalBridge(SimClient(SimWorld()).handle)
    b.connect()
    with pytest.raises(ActionError) as ei:
        b.call("login", account="AI_QA_001", password="nope")
    assert ei.value.code == "LOGIN_FAILED"


def test_qa_commands_only_for_qa_accounts():
    b = LocalBridge(SimClient(SimWorld()).handle)
    b.connect()
    b.call("login", account="PLAYER_X", password="qa")
    b.call("select_character")
    b.call("send_chat", message="/qa gold 999999")
    assert b.call("get_player_state")["gold"] == 0
    assert b.call("get_system_messages")[-1]["text"].startswith("[QA] ERR")


def test_every_sim_command_is_in_protocol():
    handlers = {n[4:] for n in dir(SimClient) if n.startswith("cmd_")}
    assert handlers == set(ALL_COMMANDS)


def test_world_is_deterministic():
    def run(seed):
        b = LocalBridge(SimClient(SimWorld(seed=seed)).handle)
        _login(b)
        return b.call("get_nearby_entities", type="monster", radius=20000)
    assert run(5) == run(5)
    assert run(5) != run(6)


def test_real_client_bridge_implements_protocol():
    """integration/client/qa_bridge.py, simülatöre özel olanlar dışında tüm komutları karşılamalı."""
    import re
    from pathlib import Path

    from qa.bridge.protocol import SIM_COMMANDS

    src = (Path(__file__).resolve().parents[1] / "integration/client/qa_bridge.py").read_text(encoding="utf-8")
    client = set(re.findall(r"^def cmd_(\w+)\(", src, re.M)) | {"wait"}
    assert set(ALL_COMMANDS) - set(SIM_COMMANDS) - client == set()
