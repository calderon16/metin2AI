// Metin2 AI QA — test hazırlık komutları: /qa <komut> ...
// ---------------------------------------------------------------------------
// Senaryonun `setup` bölümü bu komutlarla yapılır. Test ADIMLARI bu komutları kullanmaz;
// adımlar normal oyuncu paketleriyle ilerler. Böylece "hazırlık" ile "test edilen sistem" ayrışır.
//
// Güvenlik: yalnızca QA build'inde (ENABLE_AI_QA_SERVER), CONFIG'de QA_MODE: 1 iken ve
// adı AI_QA_ ile başlayan karakterler için çalışır. Diğer herkes için komut yokmuş gibi davranır.
//
// Yanıt: her zaman "[QA] OK <komut>" veya "[QA] ERR <komut>: <sebep>" (CHAT_TYPE_INFO) —
// orchestrator bunu bekler.
//
// cmd.cpp'ye ekleyin:
//     #ifdef ENABLE_AI_QA_SERVER
//     ACMD(do_qa);
//     #endif
//     ...cmd_info[] içinde:
//     #ifdef ENABLE_AI_QA_SERVER
//     { "qa", do_qa, 0, POS_DEAD, GM_PLAYER },
//     #endif
#include "stdafx.h"

#ifdef ENABLE_AI_QA_SERVER

#include "utils.h"
#include "config.h"
#include "char.h"
#include "item.h"
#include "item_manager.h"
#include "qa_event.h"

// Reset sonrası başlangıç noktası (map1 köy merkezi) — sunucunuza göre değiştirin
static const long QA_START_X = 469300;
static const long QA_START_Y = 964200;
static const int QA_START_LEVEL = 10;

static void qa_clear_inventory(LPCHARACTER ch, bool includeEquipment)
{
	for (int i = 0; i < INVENTORY_MAX_NUM; ++i)
	{
		LPITEM item = ch->GetInventoryItem(i);
		if (item)
			M2_DESTROY_ITEM(item->RemoveFromCharacter());
	}
	if (includeEquipment)
	{
		for (int i = 0; i < WEAR_MAX_NUM; ++i)
		{
			LPITEM item = ch->GetWear(i);
			if (item)
				M2_DESTROY_ITEM(item->RemoveFromCharacter());
		}
	}
}

static void qa_reply(LPCHARACTER ch, const char* cmd, const char* err = NULL)
{
	if (err)
		ch->ChatPacket(CHAT_TYPE_INFO, "[QA] ERR %s: %s", cmd, err);
	else
		ch->ChatPacket(CHAT_TYPE_INFO, "[QA] OK %s", cmd);
}

ACMD(do_qa)
{
	if (!g_bQaMode || !ch->IsQaBot())
		return; // normal oyuncu için komut yok

	char arg1[256], arg2[256], arg3[256], arg4[256];
	const char* rest = one_argument(argument, arg1, sizeof(arg1));
	rest = one_argument(rest, arg2, sizeof(arg2));
	rest = one_argument(rest, arg3, sizeof(arg3));
	one_argument(rest, arg4, sizeof(arg4));

	if (!*arg1)
	{
		qa_reply(ch, "?", "komut yok");
		return;
	}

	if (!strcmp(arg1, "reset"))
	{
		qa_clear_inventory(ch, true);
		ch->PointChange(POINT_GOLD, -ch->GetGold());
		if (ch->GetLevel() != QA_START_LEVEL)
		{
			ch->ResetPoint(QA_START_LEVEL);
			ch->ClearSkill();
			ch->ClearSubSkill();
		}
		ch->PointChange(POINT_HP, ch->GetMaxHP() - ch->GetHP());
		ch->PointChange(POINT_SP, ch->GetMaxSP() - ch->GetSP());
		// Görev bayrakları: test edilen görevleri burada sıfırlayın, ör.
		// quest::CQuestManager::instance().GetPCForce(ch->GetPlayerID())->SetFlag("dog_hunt.__status", 0);
		ch->Save();
		QA_EVENT("QA_COMMAND", ch, QA_KV("command", "reset"));
		qa_reply(ch, "reset");
		if (DISTANCE_APPROX(ch->GetX() - QA_START_X, ch->GetY() - QA_START_Y) > 1000)
			ch->WarpSet(QA_START_X, QA_START_Y);
		return;
	}

	if (!strcmp(arg1, "item"))
	{
		DWORD vnum = 0;
		int count = 1;
		str_to_number(vnum, arg2);
		if (*arg3)
			str_to_number(count, arg3);
		if (!ITEM_MANAGER::instance().GetTable(vnum))
		{
			qa_reply(ch, "item", "vnum yok");
			return;
		}
		LPITEM item = ch->AutoGiveItem(vnum, count);
		if (!item)
		{
			qa_reply(ch, "item", "envanter dolu");
			return;
		}
		QA_EVENT("QA_COMMAND", ch, QA_KV("command", "item"), QA_KV("vnum", vnum), QA_KV("count", count));
		qa_reply(ch, "item");
		return;
	}

	if (!strcmp(arg1, "gold"))
	{
		long long gold = 0;
		str_to_number(gold, arg2);
		ch->PointChange(POINT_GOLD, gold - ch->GetGold());
		qa_reply(ch, "gold");
		return;
	}

	if (!strcmp(arg1, "level"))
	{
		int level = 1;
		str_to_number(level, arg2);
		ch->ResetPoint(MINMAX(1, level, PLAYER_MAX_LEVEL_CONST));
		ch->ClearSkill();
		ch->ClearSubSkill();
		ch->PointChange(POINT_HP, ch->GetMaxHP() - ch->GetHP());
		qa_reply(ch, "level");
		return;
	}

	if (!strcmp(arg1, "hp"))
	{
		int hp = 1;
		str_to_number(hp, arg2);
		ch->PointChange(POINT_HP, MINMAX(1, hp, ch->GetMaxHP()) - ch->GetHP());
		qa_reply(ch, "hp");
		return;
	}

	if (!strcmp(arg1, "warp"))
	{
		// /qa warp <map_index> <x> <y>   (x,y: global koordinat, birim)
		long x = 0, y = 0;
		str_to_number(x, arg3);
		str_to_number(y, arg4);
		qa_reply(ch, "warp");
		ch->WarpSet(x, y);
		return;
	}

	if (!strcmp(arg1, "clear_inventory"))
	{
		qa_clear_inventory(ch, false);
		qa_reply(ch, "clear_inventory");
		return;
	}

	qa_reply(ch, arg1, "bilinmeyen komut");
}

#endif // ENABLE_AI_QA_SERVER
