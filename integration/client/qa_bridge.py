# -*- coding: utf-8 -*-
# Metin2 AI QA Bridge — istemci Python tarafı (root/qa_bridge.py)
# ---------------------------------------------------------------------------
# Python 2.7 uyumludur (klasik istemci). `json` paketinin istemcinin lib/ klasöründe olması gerekir.
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

try:
	import json
except ImportError:
	raise ImportError("qa_bridge: istemcinin lib/ klasörüne Python 2.7 'json' paketini kopyalayın")

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
	_stages[name] = window
	if name == "select" and "login" in _pending:
		_ok(_pending.pop("login"), {"characters": _characters()})


def OnLoginFailure(reason):
	if "login" in _pending:
		_err(_pending.pop("login"), "LOGIN_FAILED", str(reason))


def OnEnterGame():
	_in_game[0] = True
	_event("map_loaded", {"map": _map_name()})
	if "select" in _pending:
		_ok(_pending.pop("select"), {"name": player.GetName(), "map": _map_name()})


def OnLeaveGame():
	_in_game[0] = False
	_dialog[0] = None
	_shop[0] = None


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


def _pos():
	x, y, z = player.GetMainCharacterPosition()
	return int(x), int(y)


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
		"dead": hp <= 0, "moving": bool(chr.IsMoving()) if hasattr(chr, "IsMoving") else False,
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
	return out


def cmd_get_quest_state(a):
	out = {}
	if quest is None:
		return out
	for i in xrange(quest.GetQuestCount()):
		data = quest.GetQuestData(i)
		name, counter_name, counter_value = data[0], data[2] if len(data) > 2 else "", data[3] if len(data) > 3 else 0
		out[name] = {"state": "active", "counter": counter_name, "progress": counter_value}
	return out


def cmd_get_system_messages(a):
	return [{"seq": s, "type": t, "text": text} for s, t, text in qa.GetChat(a.get("since", 0))]


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
	grp.SaveScreenShot(path)
	if base64:
		f = open(path, "rb")
		data = f.read()
		f.close()
		return {"format": "jpg", "base64": base64.b64encode(data)}
	return {"format": "jpg", "path": path}


# ------------------------------------------------------------------ aksiyonlar
def cmd_login(a, req_id):
	stage = _stages.get("login")
	if stage is None:
		raise QaError("NO_LOGIN_STAGE", "Login ekrani acik degil")
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


def cmd_send_chat(a):
	_need_game()
	net.SendChatPacket(a["message"].encode("cp1254") if isinstance(a["message"], unicode) else a["message"])
	return {}


def cmd_respawn(a):
	_need_game()
	net.SendChatPacket("/restart_here" if a.get("here") else "/restart_town")
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
