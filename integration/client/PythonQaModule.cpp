// Metin2 AI QA — `qa` Python modülü (C++ tarafı)
// ---------------------------------------------------------------------------
// qa_bridge.py'nin mevcut Python modülleriyle (player, net, chr, item ...) okuyamadığı
// şeyleri sağlar. Aksiyonlar, normal oyuncunun fare tıklamasıyla çağrılan AYNI CPythonPlayer
// fonksiyonlarına gider; yani sunucuya giden paketler gerçek oyuncununkiyle aynıdır.
//
// Kayıt: UserInterface.cpp içinde diğer init*() çağrılarının yanına
//     #ifdef ENABLE_AI_QA_CLIENT
//         initqa();
//     #endif
//
// Aşağıdaki CPythonPlayer / CPythonItem QA_* yardımcılarını ilgili sınıflara eklemeniz gerekir
// (bkz. INTEGRATION.md "Client C++ değişiklikleri"). Fonksiyon adları fork'lar arasında
// farklılık gösterebilir; yorumdaki "orijinal yol" referansıyla eşleştirin.
#include "StdAfx.h"

#ifdef ENABLE_AI_QA_CLIENT

#include "QaBridge.h"
#include "PythonCharacterManager.h"
#include "PythonPlayer.h"
#include "PythonItem.h"
#include "InstanceBase.h"

// qa.Send(line) — orchestrator'a bir JSON satırı gönder
PyObject* qaSend(PyObject* poSelf, PyObject* poArgs)
{
	char* szLine;
	if (!PyTuple_GetString(poArgs, 0, &szLine))
		return Py_BuildException();
	CQaBridge::Instance().SendLine(szLine);
	return Py_BuildNone();
}

// qa.GetChat(since) -> [(seq, type, text), ...]
PyObject* qaGetChat(PyObject* poSelf, PyObject* poArgs)
{
	int since = 0;
	PyTuple_GetInteger(poArgs, 0, &since);
	PyObject* list = PyList_New(0);
	const auto& log = CQaBridge::Instance().GetChatLog();
	for (auto it = log.begin(); it != log.end(); ++it)
	{
		if ((int)it->first <= since)
			continue;
		PyObject* t = Py_BuildValue("(iis)", it->first, it->second.first, it->second.second.c_str());
		PyList_Append(list, t);
		Py_DECREF(t);
	}
	return list;
}

// qa.GetCharacters() -> [(vid, race, kind, x, y, name, dead), ...]
// kind: "pc" | "npc" | "monster" | "stone" | "warp" | "other"   (y oyun koordinatı: -pixel.y)
PyObject* qaGetCharacters(PyObject* poSelf, PyObject* poArgs)
{
	PyObject* list = PyList_New(0);
	CPythonCharacterManager& rkChrMgr = CPythonCharacterManager::Instance();
	CInstanceBase* pkMain = rkChrMgr.GetMainInstancePtr();

	for (CPythonCharacterManager::CharacterIterator i = rkChrMgr.CharacterInstanceBegin();
	     i != rkChrMgr.CharacterInstanceEnd(); ++i)
	{
		CInstanceBase* pInst = *i;
		if (!pInst || pInst == pkMain)
			continue;

		const char* kind = "other";
		if (pInst->IsPC()) kind = "pc";
		else if (pInst->IsNPC()) kind = "npc";
		else if (pInst->IsStone()) kind = "stone";
		else if (pInst->IsEnemy()) kind = "monster";
		else if (pInst->IsWarp()) kind = "warp";

		TPixelPosition pos;
		pInst->NEW_GetPixelPosition(&pos);
		PyObject* t = Py_BuildValue("(iisffsi)", pInst->GetVirtualID(), pInst->GetRace(), kind,
		                            pos.x, -pos.y, pInst->GetNameString(), pInst->IsDead() ? 1 : 0);
		PyList_Append(list, t);
		Py_DECREF(t);
	}
	return list;
}

// qa.GetGroundItems() -> [(vid, vnum, x, y), ...]
PyObject* qaGetGroundItems(PyObject* poSelf, PyObject* poArgs)
{
	PyObject* list = PyList_New(0);
	std::vector<CPythonItem::TQaGroundItem> items;
	CPythonItem::Instance().QA_GetGroundItems(items);
	for (size_t i = 0; i < items.size(); ++i)
	{
		PyObject* t = Py_BuildValue("(iiff)", items[i].vid, items[i].vnum, items[i].x, items[i].y);
		PyList_Append(list, t);
		Py_DECREF(t);
	}
	return list;
}

// qa.MoveTo(x, y) — yere tıklayıp yürümekle aynı yol
PyObject* qaMoveTo(PyObject* poSelf, PyObject* poArgs)
{
	float x, y;
	if (!PyTuple_GetFloat(poArgs, 0, &x) || !PyTuple_GetFloat(poArgs, 1, &y))
		return Py_BuildException();
	return Py_BuildValue("i", CPythonPlayer::Instance().QA_MoveTo(x, y) ? 1 : 0);
}

// qa.ClickActor(vid) — mob'a tıklama (hedef al + saldır) / NPC'ye tıklama (yaklaş + konuş)
PyObject* qaClickActor(PyObject* poSelf, PyObject* poArgs)
{
	int vid;
	if (!PyTuple_GetInteger(poArgs, 0, &vid))
		return Py_BuildException();
	return Py_BuildValue("i", CPythonPlayer::Instance().QA_ClickActor(vid) ? 1 : 0);
}

// qa.ClickItem(vid) — yerdeki item'e tıklama (yaklaş + topla)
PyObject* qaClickItem(PyObject* poSelf, PyObject* poArgs)
{
	int vid;
	if (!PyTuple_GetInteger(poArgs, 0, &vid))
		return Py_BuildException();
	return Py_BuildValue("i", CPythonPlayer::Instance().QA_ClickItem(vid) ? 1 : 0);
}

void initqa()
{
	static PyMethodDef s_methods[] =
	{
		{ "Send",           qaSend,           METH_VARARGS },
		{ "GetChat",        qaGetChat,        METH_VARARGS },
		{ "GetCharacters",  qaGetCharacters,  METH_VARARGS },
		{ "GetGroundItems", qaGetGroundItems, METH_VARARGS },
		{ "MoveTo",         qaMoveTo,         METH_VARARGS },
		{ "ClickActor",     qaClickActor,     METH_VARARGS },
		{ "ClickItem",      qaClickItem,      METH_VARARGS },
		{ NULL, NULL },
	};
	Py_InitModule("qa", s_methods);
}

// ---------------------------------------------------------------------------
// CPythonPlayer'a eklenecek yardımcılar (PythonPlayer.h public kısmına bildirim + bu gövdeler,
// ör. PythonPlayerInputMouse.cpp sonuna). Private __OnClickActor/__OnClickItem'a sınıf içinden
// erişilir; böylece davranış birebir oyuncunun fare tıklamasıdır.
// ---------------------------------------------------------------------------
#if 0
bool CPythonPlayer::QA_MoveTo(float x, float y)
{
	CInstanceBase* pkInstMain = NEW_GetMainActorPtr();
	if (!pkInstMain)
		return false;
	TPixelPosition kPPosDst(x, -y, 0.0f);          // oyun y -> pixel y
	// orijinal yol: __OnPressGround / NEW_MoveToDestPixelPositionDirection
	NEW_MoveToDestPixelPositionDirection(kPPosDst);
	return true;
}

bool CPythonPlayer::QA_ClickActor(DWORD vid)
{
	CInstanceBase* pkInstMain = NEW_GetMainActorPtr();
	CInstanceBase* pkInstTarget = NEW_FindActorPtr(vid);
	if (!pkInstMain || !pkInstTarget)
		return false;
	// orijinal yol: NEW_OnPressActor -> __OnClickActor (mob: hedef+saldırı, NPC: yaklaş+OnClick paketi)
	__OnClickActor(*pkInstMain, vid, false);
	return true;
}

bool CPythonPlayer::QA_ClickItem(DWORD vid)
{
	CInstanceBase* pkInstMain = NEW_GetMainActorPtr();
	if (!pkInstMain)
		return false;
	// orijinal yol: __OnPressItem -> __OnClickItem (yaklaş + SendClickItemPacket)
	__OnClickItem(*pkInstMain, vid);
	return true;
}

// PythonItem.h (public):
//     struct TQaGroundItem { DWORD vid; DWORD vnum; float x; float y; };
//     void QA_GetGroundItems(std::vector<TQaGroundItem>& out);
void CPythonItem::QA_GetGroundItems(std::vector<TQaGroundItem>& out)
{
	for (TGroundItemInstanceMap::iterator it = m_GroundItemInstanceMap.begin(); it != m_GroundItemInstanceMap.end(); ++it)
	{
		TGroundItemInstance* p = it->second;
		TQaGroundItem g = { it->first, p->dwVirtualNumber, p->v3EndPosition.x, -p->v3EndPosition.y };
		out.push_back(g);
	}
}
#endif

#endif // ENABLE_AI_QA_CLIENT
