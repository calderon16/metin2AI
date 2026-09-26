// Test fixture'ı: normal oyuncu eylemleri — NPC dükkânı, demirci/kâğıtla yükseltme (+), beceri, eşya bırakma.
// Yapılar Metin2Re istemci Packet.h ile aynı; header numaraları fixture'daki diğerleriyle çakışmasın diye farklı.
enum
{
	HEADER_CG_ITEM_DROP2 = 20,
	HEADER_CG_USE_SKILL = 52,
	HEADER_CG_ITEM_USE_TO_ITEM = 60,
	HEADER_CG_SHOP = 70,
	HEADER_CG_GIVE_ITEM = 83,
	HEADER_CG_REFINE = 96,

	HEADER_GC_SHOP = 38,
	HEADER_GC_SKILL_LEVEL_NEW = 76,
	HEADER_GC_REFINE_INFORMATION_NEW = 119,
};

#pragma pack(1)
typedef struct packet_item_attribute
{
	BYTE	bType;
	short	sValue;
} TPlayerItemAttribute;

typedef struct command_shop
{
	BYTE	header;
	BYTE	subheader;
} TPacketCGShop;

typedef struct packet_shop
{
	BYTE	header;
	WORD	size;
	BYTE	subheader;
} TPacketGCShop;

struct packet_shop_item
{
	DWORD	vnum;
	DWORD	price;
	BYTE	count;
	BYTE	display_pos;
	long	alSockets[3];
	TPlayerItemAttribute aAttr[7];
};

typedef struct command_give_item
{
	BYTE		byHeader;
	DWORD		dwTargetVID;
	TItemPos	ItemPos;
	BYTE		byItemCount;
} TPacketCGGiveItem;

typedef struct SRefineMaterial
{
	DWORD	vnum;
	int		count;
} TRefineMaterial;

typedef struct SRefineTable
{
	DWORD	src_vnum;
	DWORD	result_vnum;
	BYTE	material_count;
	int		cost;
	int		prob;
	TRefineMaterial materials[5];
} TRefineTable;

typedef struct packet_refine_information_new
{
	BYTE	header;
	BYTE	type;
	BYTE	pos;
	TRefineTable refine_table;
} TPacketGCRefineInformationNew;

typedef struct command_refine
{
	BYTE	header;
	BYTE	pos;
	BYTE	type;
} TPacketCGRefine;

typedef struct command_item_use_to_item
{
	BYTE		header;
	TItemPos	source_pos;
	TItemPos	target_pos;
} TPacketCGItemUseToItem;

typedef struct command_use_skill
{
	BYTE	bHeader;
	DWORD	dwVnum;
	DWORD	dwTargetVID;
} TPacketCGUseSkill;

typedef struct SPlayerSkill
{
	BYTE	bMasterType;
	BYTE	bLevel;
	time_t	tNextRead;
} TPlayerSkill;

typedef struct packet_skill_level_new
{
	BYTE			bHeader;
	TPlayerSkill	skills[255];
} TPacketGCSkillLevelNew;

typedef struct command_item_drop2
{
	BYTE		header;
	TItemPos	pos;
	DWORD		gold;
	BYTE		count;
} TPacketCGItemDrop2;
