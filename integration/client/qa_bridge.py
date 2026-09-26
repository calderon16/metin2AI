# -*- coding: utf-8 -*-
# Metin2 AI QA Bridge — istemci Python tarafı (root/qa_bridge.py)
# ---------------------------------------------------------------------------
# Python 2.7 uyumludur (klasik istemci). JSON için yanındaki qa_json.py kullanılır.
#
# QaBridge.cpp her satırı OnLine(line) ile iletir, her frame OnUpdate() çağırır.
# Yanıtlar qa.Send(json) ile gönderilir. Protokol: qa/bridge/protocol.py
#
# Aksiyonlar sadece normal oyuncu yollarını kullanır:
#   hareket/tıklama -> qa.MoveTo / qa.ClickActor / qa.ClickItem (CPythonPlayer'ın fare yolu)
#   item/shop/chat  -> net.Send*Packet (UI'nin kullandığı aynı paketler)
# Veritabanına ya da sunucu belleğine doğrudan erişim YOKTUR.
#
# UI kancaları (INTEGRATION.md): introLogin, introSelect, game, uiQuest, shop pencereleri
# RegisterStage / OnEnterGame / OnQuestDialog / OnShopOpen / OnShopClose / OnLoginFailure çağırır.

import app
import player
import net
import chr
import item
import grp
import qa

# İstemcinin gömülü Python'unda stdlib json çalışmayabilir (_struct yok, paket içe aktarma sorunlu)
import qa_json as json

try:
	import base64
except ImportError:
	base64 = None

try:
	import shop
except ImportError:
	shop = None

try:
	import quest
except ImportError:
	quest = None

try:
	import exchange
except ImportError:
	exchange = None

PROTOCOL_VERSION = 1
CAPABILITIES = ["screenshot"]

WEAR_NAMES = {0: "body", 1: "head", 2: "shoes", 3: "wrist", 4: "weapon", 5: "neck", 6: "ear",
              7: "unique1", 8: "unique2", 9: "arrow", 10: "shield"}
EQUIPMENT_SLOT_START = getattr(player, "EQUIPMENT_SLOT_START", 90)
INVENTORY_SIZE = getattr(player, "INVENTORY_PAGE_SIZE", 45) * getattr(player, "INVENTORY_PAGE_COUNT", 2)


class QaError(Exception):
	def __init__(self, code, message):
		Exception.__init__(self, message)
		self.code = code
		self.message = message


# ------------------------------------------------------------------ durum
_stages = {}           # "login" | "select" | "game" -> pencere nesnesi
_in_game = [False]
_waits = []            # [(bitiş_ms, req_id)]
_pending = {}          # "login" | "select" -> req_id  (UI olayına bağlı gecikmeli yanıtlar)
_target = [0]          # target komutuyla seçilen vid
_attack = [0]          # saldırı başlatılan vid (ölüm tespiti için)
_dialog = [None]       # {"text", "options", "select", "close"}
_shop = [None]         # {"vid"}
_screenshot_seq = [0]
_trade = [None]        # {"partner_vid"} — game.py StartExchange/EndExchange kancaları
_party_invite = [None] # {"leader_vid", "leader_name"}
_party = {"members": {}, "leader_pid": None}   # pid -> ad (game.py AddPartyMember/RemovePartyMember)


def _now():
	return int(app.GetTime() * 1000)


def _send(msg):
	qa.Send(json.dumps(msg))


def _ok(req_id, data):
	_send({"id": req_id, "ok": True, "t": _now(), "data": data if data is not None else {}})


def _err(req_id, code, message):
	_send({"id": req_id, "ok": False, "t": _now(), "error": {"code": code, "message": message}})


def _event(name, data):
	_send({"event": name, "t": _now(), "data": data})


def _need_game():
	if not _in_game[0]:
		raise QaError("NOT_IN_GAME", "Karakter oyunda degil")


# ------------------------------------------------------------------ UI kancaları
def RegisterStage(name, window):
	# Giriş ekranı açıldıysa önceki karakter seçim ekranı artık geçersizdir (ör. çıkış sonrası)
	if name == "login":
		_stages.pop("select", None)
	_stages[name] = window
	if name == "select" and "login" in _pending:
		_ok(_pending.pop("login"), {"characters": _characters()})


def UnregisterStage(name, window=None):
	if window is None or _stages.get(name) is window:
		_stages.pop(name, None)


def OnLoginFailure(reason):
	if "login" in _pending:
		_err(_pending.pop("login"), "LOGIN_FAILED", str(reason))


def OnEnterGame():
	_in_game[0] = True
	_hook_refine()
	_event("map_loaded", {"map": _map_name()})
	if "select" in _pending:
		_ok(_pending.pop("select"), {"name": player.GetName(), "map": _map_name()})


def OnLeaveGame():
	_in_game[0] = False
	_REFINE["info"] = None
	_dialog[0] = None
	_shop[0] = None
	_trade[0] = None
	_party_invite[0] = None


def OnQuestDialog(text, options, select_fn, close_fn):
	"""uiQuest'ten: seçenekler hazır olduğunda çağrılır. select_fn(index) normal buton tıklamasıdır."""
	_dialog[0] = {"text": text, "options": list(options), "select": select_fn, "close": close_fn}
	_event("window_opened", {"name": "dialog"})


def OnQuestDialogClosed():
	_dialog[0] = None


def OnShopOpen(vid):
	_shop[0] = {"vid": vid}
	_event("window_opened", {"name": "shop"})


def OnShopClose():
	_shop[0] = None


def OnTradeStart(partner_vid=None):
	_trade[0] = {"partner_vid": partner_vid or (_trade[0] or {}).get("partner_vid")}
	_event("trade_started", {"partner_vid": _trade[0]["partner_vid"]})


def OnTradeEnd():
	_trade[0] = None
	_event("trade_closed", {})


def OnPartyInvite(leader_vid, leader_name):
	_party_invite[0] = {"leader_vid": leader_vid, "leader_name": leader_name}
	_event("party_invite", {"leader_vid": leader_vid, "leader_name": leader_name})


def OnPartyMember(pid, name, is_leader=False):
	_party["members"][pid] = name
	if is_leader:
		_party["leader_pid"] = pid
	_event("party_joined", {"name": name, "members": len(_party["members"])})


def OnPartyMemberRemoved(pid):
	_party["members"].pop(pid, None)
	_event("party_updated", {"members": len(_party["members"])})


def OnPartyExit():
	_party["members"].clear()
	_party["leader_pid"] = None
	_event("party_left", {})


# ------------------------------------------------------------------ yardımcılar
def _map_name():
	try:
		import background
		return background.GetCurrentMapName()
	except Exception:
		return ""


def _characters():
	out = []
	for i in xrange(4):
		name = net.GetAccountCharacterSlotDataString(i, net.ACCOUNT_CHARACTER_SLOT_NAME)
		if name:
			out.append({"index": i, "name": name,
			            "level": net.GetAccountCharacterSlotDataInteger(i, net.ACCOUNT_CHARACTER_SLOT_LEVEL)})
	return out


def _entities():
	rows = []
	for vid, race, kind, x, y, name, dead in qa.GetCharacters():
		rows.append({"vid": vid, "type": kind if kind != "stone" else "monster", "vnum": race, "name": name,
		             "x": int(x), "y": int(y), "dead": bool(dead), "stone": kind == "stone"})
	for vid, vnum, x, y in qa.GetGroundItems():
		item.SelectItem(vnum)
		rows.append({"vid": vid, "type": "item", "vnum": vnum, "name": item.GetItemName(), "x": int(x), "y": int(y)})
	return rows


_last_pos = [None, 0]   # (x, y), son değişim zamanı (ms)


def _pos():
	x, y, z = player.GetMainCharacterPosition()
	pos = (int(x), int(-y))   # piksel y -> oyun y (qa.MoveTo / qa.GetCharacters ile aynı eksen)
	if pos != _last_pos[0]:
		_last_pos[0] = pos
		_last_pos[1] = _now()
	return pos


def _is_moving():
	# İstemcide chr.IsMoving yok: son 400 ms içinde konum değiştiyse hareket ediyor say
	if hasattr(chr, "IsMoving"):
		return bool(chr.IsMoving())
	return _now() - _last_pos[1] < 400


# ------------------------------------------------------------------ durum komutları
def cmd_hello(a):
	return {"client": "metin2_qa_client", "protocol": PROTOCOL_VERSION, "capabilities": CAPABILITIES}


def cmd_get_player_state(a):
	if not _in_game[0]:
		return {"in_game": False, "logged_in": "select" in _stages}
	x, y = _pos()
	hp = player.GetStatus(player.HP)
	return {
		"in_game": True, "logged_in": True, "name": player.GetName(),
		"vid": player.GetMainCharacterIndex(), "level": player.GetStatus(player.LEVEL),
		"exp": player.GetStatus(player.EXP), "hp": hp, "max_hp": player.GetStatus(player.MAX_HP),
		"sp": player.GetStatus(player.SP), "max_sp": player.GetStatus(player.MAX_SP),
		"gold": player.GetElk(), "x": x, "y": y, "map": _map_name(), "channel": net.GetServerInfo() if hasattr(net, "GetServerInfo") else None,
		"dead": hp <= 0, "moving": _is_moving(),
		"attacking": bool(_attack[0]), "target_vid": player.GetTargetVID() or None,
	}


def cmd_get_inventory(a):
	_need_game()
	items = []
	for i in xrange(INVENTORY_SIZE):
		vnum = player.GetItemIndex(i)
		if vnum:
			item.SelectItem(vnum)
			items.append({"slot": i, "vnum": vnum, "count": player.GetItemCount(i), "name": item.GetItemName()})
	eq = {}
	for w, name in WEAR_NAMES.items():
		vnum = player.GetItemIndex(EQUIPMENT_SLOT_START + w)
		if vnum:
			item.SelectItem(vnum)
			eq[name] = {"vnum": vnum, "name": item.GetItemName()}
	return {"size": INVENTORY_SIZE, "items": items, "equipment": eq}


def cmd_get_nearby_entities(a):
	_need_game()
	px, py = _pos()
	radius = a.get("radius", 5000)
	out = []
	for e in _entities():
		if e.get("dead") and e["type"] == "monster":
			continue
		if a.get("type") and e["type"] != a["type"]:
			continue
		if a.get("vnum") is not None and e["vnum"] != a["vnum"]:
			continue
		d = int(((e["x"] - px) ** 2 + (e["y"] - py) ** 2) ** 0.5)
		if d <= radius:
			e["distance"] = d
			out.append(e)
	out.sort(key=lambda r: (r["distance"], r["vid"]))
	return out


def cmd_get_target(a):
	vid = player.GetTargetVID()
	if not vid:
		return None
	for e in _entities():
		if e["vid"] == vid:
			return {"vid": vid, "type": e["type"], "vnum": e["vnum"], "name": e["name"], "dead": e.get("dead", False)}
	return {"vid": vid, "dead": True}


def cmd_get_open_windows(a):
	out = []
	if _dialog[0]:
		out.append({"name": "dialog", "text": _dialog[0]["text"], "options": _dialog[0]["options"]})
	if _shop[0] and shop:
		items = []
		for i in xrange(getattr(shop, "SHOP_SLOT_COUNT", 40)):
			vnum = shop.GetItemID(i)
			if vnum:
				item.SelectItem(vnum)
				items.append({"slot": i, "vnum": vnum, "name": item.GetItemName(), "price": shop.GetItemPrice(i)})
		out.append({"name": "shop", "npc_vid": _shop[0]["vid"], "items": items})
	if _trade[0] and exchange:
		out.append(dict({"name": "trade"}, **_trade_view()))
	if _party_invite[0]:
		out.append(dict({"name": "party_invite"}, **_party_invite[0]))
	if _REFINE["info"] is not None:
		out.append(dict({"name": "refine"}, **_REFINE["info"]))
	return out


def _trade_view():
	n = getattr(exchange, "EXCHANGE_ITEM_MAX_NUM", 12)

	def items(get_vnum, get_count):
		out = []
		for i in xrange(n):
			vnum = get_vnum(i)
			if vnum:
				item.SelectItem(vnum)
				out.append({"slot": i, "vnum": vnum, "count": get_count(i), "name": item.GetItemName()})
		return out

	return {
		"partner_vid": _trade[0].get("partner_vid"),
		"partner_name": exchange.GetNameFromTarget() if hasattr(exchange, "GetNameFromTarget") else None,
		"my_items": items(exchange.GetItemVnumFromSelf, exchange.GetItemCountFromSelf),
		"their_items": items(exchange.GetItemVnumFromTarget, exchange.GetItemCountFromTarget),
		"my_gold": exchange.GetElkFromSelf(), "their_gold": exchange.GetElkFromTarget(),
		"my_accepted": bool(exchange.GetAcceptFromSelf()), "their_accepted": bool(exchange.GetAcceptFromTarget()),
	}


def cmd_get_party(a):
	_need_game()
	if not _party["members"]:
		return {"in_party": False, "members": []}
	my_pid = player.GetPlayerID() if hasattr(player, "GetPlayerID") else None
	leader_name = _party["members"].get(_party["leader_pid"])
	leader_vid = None
	for e in _entities():
		if e["type"] == "pc" and e["name"] == leader_name:
			leader_vid = e["vid"]
	if leader_name == player.GetName():
		leader_vid = player.GetMainCharacterIndex()
	return {"in_party": True, "leader_vid": leader_vid, "leader_name": leader_name,
	        "is_leader": my_pid is not None and my_pid == _party["leader_pid"],
	        "members": [{"name": n} for n in _party["members"].values()]}


def cmd_get_quest_state(a):
	out = {}
	if quest is None:
		return out
	for i in xrange(quest.GetQuestCount()):
		data = quest.GetQuestData(i)
		name, counter_name, counter_value = data[0], data[2] if len(data) > 2 else "", data[3] if len(data) > 3 else 0
		out[name] = {"state": "active", "counter": counter_name, "progress": counter_value}
	# Metin2Re görev motoru (mr2quest): kimliğe göre, hedef satırlarıyla
	try:
		import mr2quest
	except ImportError:
		return out
	for qid, q in mr2quest.QUESTS.items():
		objs = [{"label": label, "cur": cur, "max": maxv} for idx, (cur, maxv, label) in sorted(q["objs"].items())]
		entry = {"state": "ready" if q.get("status") == 2 else "active", "title": q.get("title", ""),
		         "progress": sum([min(o["cur"], o["max"]) for o in objs]), "objs": objs,
		         "marked": q.get("mark") is not None}
		mark = q.get("mark")
		if mark is not None:
			# İşaretli NPC (ya da ışınlayıcı) köprü koordinatında: x yerel, y = -yerel y (_pos ile aynı)
			npc, vid, gx, gy, gate = mark
			try:
				import background
				lx, ly = background.GlobalPositionToLocalPosition(gx, gy)
				entry["target"] = {"npc": npc, "vid": vid, "x": lx, "y": -ly, "gate": bool(gate)}
			except Exception:
				pass
			if mr2quest.GuideQuest() == qid:
				try:
					import questguide
					entry["path"] = [{"x": x, "y": -y} for x, y in questguide.GetPath()]
				except Exception:
					pass
		out[qid] = entry
	for qid, a in mr2quest.AVAIL.items():
		if qid in out:
			continue
		entry = {"state": "available", "title": a["title"], "progress": 0, "marked": True}
		try:
			import background
			lx, ly = background.GlobalPositionToLocalPosition(a["x"], a["y"])
			entry["target"] = {"npc": a["npc"], "vid": a["vid"], "x": lx, "y": -ly, "gate": bool(a["gate"])}
		except Exception:
			pass
		if mr2quest.GuideQuest() == qid:
			try:
				import questguide
				entry["path"] = [{"x": x, "y": -y} for x, y in questguide.GetPath()]
			except Exception:
				pass
		out[qid] = entry
	for qid in mr2quest.DONE:
		if qid not in out:
			out[qid] = {"state": "done", "progress": 0}
	return out


def cmd_get_system_messages(a):
	return [{"seq": s, "type": t, "text": text} for s, t, text in qa.GetChat(a.get("since", 0))]


_WHISPERS = []
_WSP = {"seq": 0, "hooked": False}


def _hook_whispers():
	"""game.GameWindow.OnRecvWhisper'i sar: gelen fısıltıları köprü için biriktir (C++ değişikliği gerekmez)."""
	if _WSP["hooked"]:
		return
	try:
		import game
	except ImportError:
		return
	orig = getattr(game.GameWindow, "OnRecvWhisper", None)
	if orig is None:
		return

	def OnRecvWhisper(self, mode, name, line):
		try:
			text = line
			if " : " in line:
				text = line.split(" : ", 1)[1]
			_WSP["seq"] += 1
			_WHISPERS.append({"seq": _WSP["seq"], "from": name, "text": text, "gm": mode == 5})
			del _WHISPERS[:-200]
		except Exception:
			pass
		return orig(self, mode, name, line)

	game.GameWindow.OnRecvWhisper = OnRecvWhisper
	_WSP["hooked"] = True


def cmd_get_whispers(a):
	_hook_whispers()
	since = a.get("since", 0)
	return [w for w in _WHISPERS if w["seq"] > since]


def cmd_whisper(a):
	_need_game()
	_hook_whispers()
	net.SendWhisperPacket(json.encode_cp1254(a["to"]), json.encode_cp1254(a["message"]))
	return {}


def cmd_get_client_log(a):
	try:
		f = open("syserr.txt", "r")
		lines = f.read().splitlines()[-200:]
		f.close()
	except IOError:
		lines = []
	return [{"seq": i + 1, "t": 0, "text": l} for i, l in enumerate(lines) if i + 1 > a.get("since", 0)]


def cmd_screenshot(a):
	_screenshot_seq[0] += 1
	path = "screenshot/qa_%05d.jpg" % _screenshot_seq[0]
	if hasattr(grp, "SaveScreenShotToPath"):
		# grp.SaveScreenShot yolu yok sayıp Belgeler\METIN2'ye yazar; ToPath verilen önekle kaydeder
		ok, path = grp.SaveScreenShotToPath("screenshot/qa_%05d_" % _screenshot_seq[0])
		if not ok:
			raise QaError("SCREENSHOT_FAILED", "Ekran goruntusu kaydedilemedi")
	else:
		grp.SaveScreenShot(path)
	import os
	path = os.path.abspath(path)   # orchestrator başka klasörde çalışır
	if base64:
		f = open(path, "rb")
		data = f.read()
		f.close()
		return {"format": "jpg", "base64": base64.b64encode(data)}
	return {"format": "jpg", "path": path}


# ------------------------------------------------------------------ aksiyonlar
def _select_channel(stage, wanted=None):
	import serverInfo
	region = stage._LoginWindow__GetRegionID()
	server = stage._LoginWindow__GetServerID()
	try:
		channels = serverInfo.REGION_DICT[region][server]["channel"]
	except KeyError:
		raise QaError("NO_SERVER", "Sunucu listesinde secili sunucu yok")
	open_states = (serverInfo.STATE_DICT[1], serverInfo.STATE_DICT[2])
	order = ([int(wanted)] if wanted else []) + sorted(channels.keys())
	for channel_id in order:
		if channel_id in channels and channels[channel_id].get("state") in open_states:
			stage.channelList.SelectItem(channel_id - 1)
			return channel_id
	raise QaError("NO_CHANNEL", "Acik kanal yok (sunucu durumu henuz gelmemis olabilir)")


def cmd_login(a, req_id):
	stage = _stages.get("login")
	if stage is None:
		raise QaError("NO_LOGIN_STAGE", "Login ekrani acik degil")
	board = getattr(stage, "serverBoard", None)
	if board is not None and board.IsShow():
		# Sunucu/kanal panosu açık: çalışan bir kanal seçip "Tamam"a basan yolla giriş panosuna geç
		_select_channel(stage, a.get("channel"))
		stage._LoginWindow__OnClickSelectServerButton()
	stage.idEditLine.SetText(a["account"])
	stage.pwdEditLine.SetText(a["password"])
	_pending["login"] = req_id
	stage._LoginWindow__OnClickLoginButton()   # normal "Giriş" butonu
	return _DEFERRED


def cmd_select_character(a, req_id):
	stage = _stages.get("select")
	if stage is None:
		raise QaError("NOT_LOGGED_IN", "Karakter secim ekrani acik degil")
	index = a.get("index")
	if a.get("name") is not None:
		matches = [c["index"] for c in _characters() if c["name"] == a["name"]]
		if not matches:
			raise QaError("NO_CHARACTER", "Karakter bulunamadi")
		index = matches[0]
	_pending["select"] = req_id
	stage.SelectSlot(index or 0)
	stage.StartGame()
	return _DEFERRED


def cmd_logout(a):
	_need_game()
	net.LogOutGame()
	OnLeaveGame()
	return {}


def cmd_move_to(a):
	_need_game()
	if not qa.MoveTo(float(a["x"]), float(a["y"])):
		raise QaError("MOVE_FAILED", "Hareket baslatilamadi")
	_attack[0] = 0
	return {}


def cmd_target(a):
	_need_game()
	_target[0] = int(a["vid"])
	return {}


def cmd_attack(a):
	_need_game()
	if not _target[0]:
		raise QaError("NO_TARGET", "Hedef secilmedi")
	if not qa.ClickActor(_target[0]):
		raise QaError("NO_ENTITY", "Hedef bulunamadi")
	_attack[0] = _target[0]
	return {}


def cmd_stop_attack(a):
	_attack[0] = 0
	return {}


def cmd_use_skill(a):
	_need_game()
	player.ClickSkillSlot(int(a["slot"]))
	return {}


def cmd_use_item(a):
	_need_game()
	net.SendItemUsePacket(int(a["slot"]))
	return {}


def cmd_equip_item(a):
	return cmd_use_item(a)   # Metin2'de giyilebilir item kullanılınca giyilir


def cmd_unequip_item(a):
	_need_game()
	wear = [k for k, v in WEAR_NAMES.items() if v == a["wear_slot"]]
	if not wear:
		raise QaError("BAD_ARGS", "Bilinmeyen wear slot")
	net.SendItemUsePacket(EQUIPMENT_SLOT_START + wear[0])  # giyili item'e sağ tık = çıkar
	return {}


def cmd_split_item(a):
	_need_game()
	slot, count = int(a["slot"]), int(a["count"])
	have = player.GetItemCount(slot)
	if not 0 < count < have:
		raise QaError("BAD_COUNT", "Bolmek icin 1..%d arasi adet gerekli" % (have - 1))
	free = None
	for i in xrange(EQUIPMENT_SLOT_START):
		if not player.GetItemIndex(i):
			free = i
			break
	if free is None:
		raise QaError("INVENTORY_FULL", "Envanterde bos yer yok")
	net.SendItemMovePacket(slot, free, count)   # istemcide Shift+surukle
	return {"slot": free, "count": count}


def cmd_drop_item(a):
	_need_game()
	slot = int(a["slot"])
	net.SendItemDropPacketNew(slot, int(a.get("count") or player.GetItemCount(slot)))
	return {}


def cmd_pickup(a):
	_need_game()
	if not qa.ClickItem(int(a["vid"])):
		raise QaError("NO_ENTITY", "Item bulunamadi")
	return {}


def cmd_talk_to_npc(a):
	_need_game()
	if not qa.ClickActor(int(a["vid"])):
		raise QaError("NO_ENTITY", "NPC bulunamadi")
	return {}


def cmd_select_dialog(a):
	d = _dialog[0]
	if not d:
		raise QaError("NO_DIALOG", "Acik dialog yok")
	i = int(a["index"])
	if not 0 <= i < len(d["options"]):
		raise QaError("BAD_OPTION", "Gecersiz secenek")
	_dialog[0] = None
	d["select"](i)
	return {"selected": d["options"][i]}


def cmd_close_window(a):
	if a["name"] == "dialog" and _dialog[0]:
		_dialog[0]["close"]()
		_dialog[0] = None
	elif a["name"] == "shop" and _shop[0]:
		net.SendShopEndPacket()
		_shop[0] = None
	elif a["name"] == "trade" and _trade[0]:
		net.SendExchangeExitPacket()
	elif a["name"] == "party_invite" and _party_invite[0]:
		return cmd_party_answer({"accept": False})
	elif a["name"] == "refine" and _REFINE["info"] is not None:
		return cmd_refine_item({"slot": _REFINE["info"]["slot"], "confirm": False})
	return {}


def cmd_buy_item(a):
	if not _shop[0]:
		raise QaError("NO_SHOP", "Acik dukkan yok")
	net.SendShopBuyPacket(int(a["slot"]))
	return {}


def cmd_sell_item(a):
	if not _shop[0]:
		raise QaError("NO_SHOP", "Acik dukkan yok")
	slot = int(a["slot"])
	net.SendShopSellPacketNew(slot, int(a.get("count") or player.GetItemCount(slot)))
	return {}


# ------------------------------------------------------------------ yükseltme (+)
_REFINE = {"hooked": False, "win": None, "info": None}


def _hook_refine():
	"""game.GameWindow'un yükseltme kancalarını sar: açılan pencere bilgisi ve sonuç olayı (C++ değişmez)."""
	if _REFINE["hooked"]:
		return
	try:
		import game
	except ImportError:
		return
	gw = game.GameWindow
	o_open = getattr(gw, "OpenRefineDialog", None)
	o_mat = getattr(gw, "AppendMaterialToRefineDialog", None)
	o_ok = getattr(gw, "RefineSuceededMessage", None)
	o_fail = getattr(gw, "RefineFailedMessage", None)
	if o_open is None:
		return

	def OpenRefineDialog(self, targetItemPos, nextGradeItemVnum, cost, prob, type=0):
		_REFINE["win"] = self
		_REFINE["info"] = {"slot": targetItemPos, "src_vnum": player.GetItemIndex(targetItemPos),
			"result_vnum": nextGradeItemVnum, "cost": cost, "prob": prob, "type": type, "materials": []}
		_event("window_opened", {"name": "refine"})
		return o_open(self, targetItemPos, nextGradeItemVnum, cost, prob, type)

	def AppendMaterialToRefineDialog(self, vnum, count):
		if _REFINE["info"] is not None:
			_REFINE["info"]["materials"].append({"vnum": vnum, "count": count})
		return o_mat(self, vnum, count)

	def RefineSuceededMessage(self):
		_event("refine_result", {"result": "success"})
		return o_ok(self)

	def RefineFailedMessage(self):
		_event("refine_result", {"result": "failed"})
		return o_fail(self)

	gw.OpenRefineDialog = OpenRefineDialog
	if o_mat is not None:
		gw.AppendMaterialToRefineDialog = AppendMaterialToRefineDialog
	if o_ok is not None:
		gw.RefineSuceededMessage = RefineSuceededMessage
	if o_fail is not None:
		gw.RefineFailedMessage = RefineFailedMessage
	_REFINE["hooked"] = True


def _refine_dialog():
	w = _REFINE["win"]
	try:
		return w.interface.dlgRefineNew
	except AttributeError:
		return None


def cmd_refine_item(a):
	"""Iki adim: pencere yoksa esyayi demirciye verir / kagidi esyaya surukler ({pending: true});
	pencere acildiktan sonra ayni komut onaylar (sonuc: refine_result olayi) ya da vazgecer."""
	_need_game()
	_hook_refine()
	slot = int(a["slot"])
	info = _REFINE["info"]
	if info is not None and info["slot"] == slot:
		_REFINE["info"] = None
		dlg = _refine_dialog()
		out = dict(info)
		out.pop("slot", None)
		if not a.get("confirm", True):
			if dlg is not None:
				dlg.CancelRefine()
			else:
				net.SendRefinePacket(255, 255)
			out["confirmed"] = False
			return out
		if dlg is not None:
			dlg.Accept()
		else:
			net.SendRefinePacket(slot, info["type"])
		out["confirmed"] = True
		out["pending"] = True
		return out
	if a.get("scroll_slot") is not None:
		net.SendItemUseToItemPacket(int(a["scroll_slot"]), slot)
	elif a.get("npc_vid") is not None:
		net.SendGiveItemPacket(int(a["npc_vid"]), slot, 1)
	else:
		raise QaError("BAD_ARGS", "npc_vid ya da scroll_slot gerekli")
	return {"pending": True}


def cmd_send_chat(a):
	_need_game()
	net.SendChatPacket(json.encode_cp1254(a["message"]))
	return {}


def cmd_respawn(a):
	_need_game()
	net.SendChatPacket("/restart_here" if a.get("here") else "/restart_town")
	return {}


def cmd_trade_request(a):
	_need_game()
	_trade[0] = {"partner_vid": int(a["vid"])}   # pencere StartExchange kancasıyla açılır
	net.SendExchangeStartPacket(int(a["vid"]))
	return {}


def _need_trade():
	if not _trade[0]:
		raise QaError("NO_TRADE", "Acik ticaret yok")


def cmd_trade_add_item(a):
	_need_trade()
	used = len(_trade_view()["my_items"])
	net.SendExchangeItemAddPacket(int(a["slot"]), used)   # (envanter slotu, ticaret penceresi slotu)
	return {}


def cmd_trade_set_gold(a):
	_need_trade()
	if int(a["amount"]) > player.GetElk():
		raise QaError("NOT_ENOUGH_GOLD", "Yetersiz yang")
	net.SendExchangeElkAddPacket(int(a["amount"]))
	return {}


def cmd_trade_accept(a):
	_need_trade()
	net.SendExchangeAcceptPacket()
	return {"completed": None}   # sonuç pencerenin kapanması / sunucu TRADE_COMPLETE olayı ile anlaşılır


def cmd_trade_cancel(a):
	_need_trade()
	net.SendExchangeExitPacket()
	return {}


def cmd_party_invite(a):
	_need_game()
	net.SendPartyInvitePacket(int(a["vid"]))
	return {}


def cmd_party_answer(a):
	inv = _party_invite[0]
	if not inv:
		raise QaError("NO_INVITE", "Bekleyen grup daveti yok")
	_party_invite[0] = None
	net.SendPartyInviteAnswerPacket(inv["leader_vid"], 1 if a.get("accept", True) else 0)
	return {"joined": None}


def cmd_party_leave(a):
	if not _party["members"]:
		raise QaError("NOT_IN_PARTY", "Grupta degilsin")
	net.SendPartyExitPacket()
	return {}


def cmd_party_kick(a):
	vid = int(a["vid"])
	name = [e["name"] for e in _entities() if e["vid"] == vid]
	pids = [pid for pid, n in _party["members"].items() if name and n == name[0]]
	if not pids:
		raise QaError("NOT_MEMBER", "Bu oyuncu grupta degil")
	net.SendPartyRemovePacket(pids[0])
	return {}


def cmd_change_channel(a):
	raise QaError("NOT_SUPPORTED", "Kanal degistirme henuz QA client'ta yok")


_DEFERRED = object()
_WITH_ID = ("login", "select_character")


# ------------------------------------------------------------------ giriş noktaları (C++'tan)
def OnConnect():
	del _waits[:]
	_pending.clear()


def OnDisconnect():
	del _waits[:]
	_pending.clear()


def OnLine(line):
	req_id = None
	try:
		msg = json.loads(line)
		req_id = msg.get("id")
		cmd = msg.get("cmd", "")
		args = msg.get("args") or {}
		if cmd == "wait":
			_waits.append((_now() + int(args.get("ms", 0)), req_id))
			return
		fn = globals().get("cmd_" + cmd)
		if fn is None:
			_err(req_id, "UNKNOWN_COMMAND", "Bilinmeyen komut: %s" % cmd)
			return
		data = fn(args, req_id) if cmd in _WITH_ID else fn(args)
		if data is not _DEFERRED:
			_ok(req_id, data)
	except QaError, e:
		_err(req_id, e.code, e.message)
	except (KeyError, ValueError, TypeError), e:
		_err(req_id, "BAD_ARGS", str(e))
	except Exception, e:
		import dbg
		dbg.TraceError("qa_bridge: %s" % e)
		_err(req_id, "CLIENT_ERROR", str(e))


def OnUpdate():
	now = _now()
	if _waits:
		due = [w for w in _waits if w[0] <= now]
		for w in due:
			_waits.remove(w)
			_ok(w[1], {})
	# Saldırılan hedef öldü mü / kayboldu mu?
	if _attack[0] and _in_game[0]:
		vid = _attack[0]
		alive = [c for c in qa.GetCharacters() if c[0] == vid and not c[6]]
		if not alive:
			race = [c[1] for c in qa.GetCharacters() if c[0] == vid]
			_attack[0] = 0
			_event("entity_dead", {"vid": vid, "vnum": race[0] if race else None})
