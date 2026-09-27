"""Opt-in real-server acceptance using a dedicated existing QA character."""
import json
from pathlib import Path
from qa.config import load_config
from qa.headless.client import HeadlessClient

cfg=load_config(Path('qa.local.toml'))
client=HeadlessClient(cfg.headless,resolve=cfg.resolve)
report={}
def connect():
    client.cmd_login('AI_QA_004',cfg.accounts.password)
    client.cmd_select_character(index=0)
    client.cmd_wait(1500)
try:
    connect()
    report['initial']=client.cmd_mount_command('list')
    for kind,vnum in ((1,71300),(2,71301)):
        if not report['initial']['kinds'][kind-1]['own']:
            client.cmd_send_chat('/qa item %d 1'%vnum)
            client.cmd_wait(350)
            slot=next(i['slot'] for i in client.cmd_get_inventory()['items'] if i['vnum']==vnum)
            client.cmd_use_item(slot)
            client.cmd_wait(350)
    report['summon']=client.cmd_mount_command('summon',1)
    assert report['summon']['summoned']==1
    report['follower']=client.cmd_get_nearby_entities(vnum=20110)
    assert report['follower'], 'Follower entity missing'
    position=client.cmd_get_player_state()
    client.cmd_move_to(position['x']+1400,position['y'])
    client.cmd_wait(11000)
    report['player_after_move']=client.cmd_get_player_state()
    report['follow_after_move']=client.cmd_get_nearby_entities(vnum=20110)
    assert report['follow_after_move'], 'Follower missing after moving'
    first,last=report['follower'][0],report['follow_after_move'][0]
    assert abs(first['x']-last['x'])+abs(first['y']-last['y'])>100, 'Follower did not move'
    report['appearance']=client.cmd_mount_command('appearance',2)
    assert report['appearance']['appearance']==2
    report['appearance_follower']=client.cmd_get_nearby_entities(vnum=20111)
    assert report['appearance_follower'], 'Appearance follower missing'
    client.cmd_send_chat('/qa item 71310 1')
    client.cmd_wait(350)
    before=client.cmd_mount_command('list')['kinds'][0]
    report['feed']=client.cmd_mount_command('feed',1)
    after=report['feed']['kinds'][0]
    assert (after['level'],after['xp'])>(before['level'],before['xp'])
    client._drop_connection()
    connect()
    report['relogin']=client.cmd_mount_command('list')
    assert report['relogin']['summoned']==1 and report['relogin']['appearance']==2
    assert client.cmd_get_nearby_entities(vnum=20111), 'Relog follower missing'
    report['dismiss']=client.cmd_mount_command('dismiss')
    assert report['dismiss']['summoned']==0
    client.cmd_wait(350)
    assert not client.cmd_get_nearby_entities(vnum=20111), 'Dismissed follower remains'
    report['result']='PASS'
finally:
    client._drop_connection()
    destination=Path('../build/mount-stable/live-acceptance.json')
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
