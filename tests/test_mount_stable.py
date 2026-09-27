import pytest

from qa.sim.world import SimClient, SimWorld, SimError
from qa.headless.client import HeadlessClient


def test_mount_state_packet_keeps_list():
    client = object.__new__(HeadlessClient)
    client.mounts = None
    client._on_mr2('MR_MOUNT_LIST 0 1:1:0:5:3')
    client._on_mr2('MR_MOUNT_STATE 1 1 0')
    assert client.mounts['summoned'] == 1
    assert client.mounts['appearance'] == 1
    assert client.mounts['riding'] is False
    assert client.mounts['kinds'][0]['bonus'] == 3


def test_sim_mount_ownership_and_lifecycle():
    client = SimClient(SimWorld())
    client.cmd_login(account='AI_QA_001', password='qa')
    client.cmd_select_character()
    with pytest.raises(SimError) as error:
        client.cmd_mount_command('summon', 1)
    assert error.value.code == 'NOT_OWNED'
    mounts = client.cmd_get_mounts()
    mounts['kinds'][0].update(own=True, level=1, bonus=3)
    assert client.cmd_mount_command('summon', 1)['summoned'] == 1
    assert client.cmd_mount_command('appearance', 1)['appearance'] == 1
    with pytest.raises(SimError) as error:
        client.cmd_mount_command('ride', 1)
    assert error.value.code == 'NOT_SUPPORTED'
    assert client.cmd_mount_command('dismount')['summoned'] == 1
    result = client.cmd_mount_command('dismiss')
    assert (result['active'], result['summoned'], result['appearance'], result['riding']) == (0, 0, 0, False)
    assert result['kinds'][0]['bonus'] == 3


def test_headless_waits_for_state_after_list():
    client = object.__new__(HeadlessClient)
    client.mounts = None
    client._msg_seq = 0
    client._need_game = lambda: None
    client._chat_command = lambda command: None
    client._msgs_since = lambda before: []
    waits = []

    def pump(predicate, timeout):
        waits.append(timeout)
        if len(waits) == 1:
            client._on_mr2('MR_MOUNT_LIST 0 1:1:0:5:3')
        else:
            assert not predicate()
            client._on_mr2('MR_MOUNT_STATE 1 1 0')
        return predicate()

    client._pump_for = pump
    result = client.cmd_mount_command('summon', 1)
    assert result['summoned'] == 1 and result['riding'] is False
    assert waits == [3.0, 0.3]
