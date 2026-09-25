// Test fixture'ı: klasik Metin2 packet.h yapısında küçük bir alt küme.
// Gerçek fork'un packet.h dosyası `metin2-qa packets import` ile aynı şekilde ayrıştırılır.
#ifndef __INC_PACKET_H__
#define __INC_PACKET_H__

#define CHARACTER_NAME_MAX_LEN 24
#define LOGIN_MAX_LEN 30
#define PASSWD_MAX_LEN 16
#define PLAYER_PER_ACCOUNT 4
#define CHAT_MAX_LEN 512
#define POINT_MAX_NUM 255

enum
{
	HEADER_CG_HANDSHAKE = 0xff,
	HEADER_CG_PONG = 0xfe,
	HEADER_CG_LOGIN2 = 109,
	HEADER_CG_LOGIN3 = 111,
	HEADER_CG_ATTACK = 2,
	HEADER_CG_CHAT,                 // 3 (örtük artış)
	HEADER_CG_CHARACTER_SELECT = 6,
	HEADER_CG_ENTERGAME = 10,
	HEADER_CG_ITEM_USE = 11,
	HEADER_CG_ITEM_PICKUP = 15,
	HEADER_CG_ON_CLICK = 26,
	HEADER_CG_SCRIPT_ANSWER = 29,
	HEADER_CG_TARGET = 61,
	HEADER_CG_MOVE = 7,

	HEADER_GC_HANDSHAKE = 0xff,
	HEADER_GC_PING = 0x2c,
	HEADER_GC_PHASE = 0xfd,
	HEADER_GC_AUTH_SUCCESS = 150,
	HEADER_GC_LOGIN_FAILURE = 7,
	HEADER_GC_LOGIN_SUCCESS4 = 32,
	HEADER_GC_MAIN_CHARACTER = 113,
	HEADER_GC_CHARACTER_POINTS = 16,
	HEADER_GC_CHARACTER_POINT_CHANGE = 17,
	HEADER_GC_CHARACTER_ADD = 1,
	HEADER_GC_CHAR_ADDITIONAL_INFO = 136,
	HEADER_GC_CHARACTER_DEL = 2,
	HEADER_GC_MOVE = 3,
	HEADER_GC_CHAT = 4,
	HEADER_GC_DEAD = 14,
	HEADER_GC_ITEM_SET = 21,
	HEADER_GC_ITEM_GROUND_ADD = 26,
	HEADER_GC_ITEM_GROUND_DEL = 27,
	HEADER_GC_SCRIPT = 45,
};

enum EPhases
{
	PHASE_CLOSE,
	PHASE_HANDSHAKE,
	PHASE_LOGIN,
	PHASE_SELECT,
	PHASE_LOADING,
	PHASE_GAME,
	PHASE_DEAD,
	PHASE_AUTH = 10,
};

#pragma pack(1)

typedef struct SPacketGCHandshake
{
	BYTE	bHeader;
	DWORD	dwHandshake;
	DWORD	dwTime;
	long	lDelta;
} TPacketGCHandshake;

typedef struct packet_phase
{
	BYTE	header;
	BYTE	phase;
} TPacketGCPhase;

typedef struct packet_header_only
{
	BYTE	header;
} TPacketCGHeader;

typedef struct command_login3
{
	BYTE	header;
	char	login[LOGIN_MAX_LEN + 1];
	char	passwd[PASSWD_MAX_LEN + 1];
	DWORD	adwClientKey[4];
} TPacketCGLogin3;

typedef struct command_login2
{
	BYTE	header;
	char	login[LOGIN_MAX_LEN + 1];
	DWORD	dwLoginKey;
	DWORD	adwClientKey[4];
} TPacketCGLogin2;

typedef struct packet_auth_success
{
	BYTE	bHeader;
	DWORD	dwLoginKey;
	BYTE	bResult;
} TPacketGCAuthSuccess;

typedef struct packet_login_failure
{
	BYTE	header;
	char	szStatus[9];
} TPacketGCLoginFailure;

typedef struct SSimplePlayer
{
	DWORD	dwID;
	char	szName[CHARACTER_NAME_MAX_LEN + 1];
	BYTE	byJob;
	BYTE	byLevel;
	DWORD	dwPlayMinutes;
	BYTE	byST, byHT, byDX, byIQ;
	WORD	wMainPart;
	BYTE	bChangeName;
	WORD	wHairPart;
	BYTE	bDummy[4];
	long	x, y;
	long	lAddr;
	WORD	wPort;
	BYTE	skill_group;
} TSimplePlayer;

typedef struct packet_login_success4
{
	BYTE		bHeader;
	TSimplePlayer	players[PLAYER_PER_ACCOUNT];
	DWORD		guild_id[PLAYER_PER_ACCOUNT];
	char		guild_name[PLAYER_PER_ACCOUNT][13];
	DWORD		handle;
	DWORD		random_key;
} TPacketGCLoginSuccess4;

typedef struct command_player_select
{
	BYTE	header;
	BYTE	index;
} TPacketCGPlayerSelect;

typedef struct packet_main_character
{
	BYTE	header;
	DWORD	dwVID;
	WORD	wRaceNum;
	char	szName[CHARACTER_NAME_MAX_LEN + 1];
	long	lx, ly, lz;
	BYTE	empire;
	BYTE	skill_group;
} TPacketGCMainCharacter;

typedef struct packet_points
{
	BYTE	header;
	INT	points[POINT_MAX_NUM];
} TPacketGCPoints;

typedef struct packet_point_change
{
	int		header;      // bilerek yanlış tip değil: bazı fork'larda farklıdır, burada BYTE kullanılır
} TPacketDummyUnused;

typedef struct packet_point_change2
{
	BYTE	header;
	DWORD	dwVID;
	BYTE	type;
	long	amount;
	long	value;
} TPacketGCPointChange;

typedef struct packet_add_char
{
	BYTE	header;
	DWORD	dwVID;
	float	angle;
	long	x, y, z;
	BYTE	bType;
	WORD	wRaceNum;
	BYTE	bMovingSpeed;
	BYTE	bAttackSpeed;
	BYTE	bStateFlag;
	DWORD	dwAffectFlag[2];
} TPacketGCCharacterAdd;

typedef struct packet_char_additional_info
{
	BYTE	header;
	DWORD	dwVID;
	char	name[CHARACTER_NAME_MAX_LEN + 1];
	WORD	awPart[4];
	BYTE	bEmpire;
	DWORD	dwGuildID;
	DWORD	dwLevel;
	short	sAlignment;
	BYTE	bPKMode;
	DWORD	dwMountVnum;
} TPacketGCCharacterAdditionalInfo;

typedef struct packet_del_char
{
	BYTE	header;
	DWORD	id;
} TPacketGCCharacterDelete;

typedef struct command_move
{
	BYTE	bHeader;
	BYTE	bFunc;
	BYTE	bArg;
	BYTE	bRot;
	long	lX;
	long	lY;
	DWORD	dwTime;
} TPacketCGMove;

typedef struct packet_move
{
	BYTE	bHeader;
	BYTE	bFunc;
	BYTE	bArg;
	BYTE	bRot;
	DWORD	dwVID;
	long	lX;
	long	lY;
	DWORD	dwTime;
	DWORD	dwDuration;
} TPacketGCMove;

typedef struct command_chat
{
	BYTE	header;
	WORD	size;
	BYTE	type;
} TPacketCGChat;

typedef struct packet_chatting
{
	BYTE	header;
	WORD	size;
	BYTE	type;
	DWORD	id;
	BYTE	bEmpire;
} TPacketGCChat;

typedef struct command_attack
{
	BYTE	bHeader;
	BYTE	bType;
	DWORD	dwVID;
	BYTE	bCRCMagicCubeProcPiece;
	BYTE	bCRCMagicCubeFilePiece;
} TPacketCGAttack;

typedef struct command_target
{
	BYTE	header;
	DWORD	dwVID;
} TPacketCGTarget;

typedef struct packet_dead
{
	BYTE	header;
	DWORD	vid;
} TPacketGCDead;

typedef struct SItemPos
{
	BYTE	window_type;
	WORD	cell;
} TItemPos;

typedef struct command_item_use
{
	BYTE		header;
	TItemPos	Cell;
} TPacketCGItemUse;

typedef struct command_item_pickup
{
	BYTE	header;
	DWORD	vid;
} TPacketCGItemPickup;

typedef struct packet_item_set
{
	BYTE		header;
	TItemPos	Cell;
	DWORD		vnum;
	BYTE		count;
	DWORD		flags;
	DWORD		anti_flags;
	bool		highlight;
	long		alSockets[3];
} TPacketGCItemSet;

typedef struct packet_ground_add_item
{
	BYTE	bHeader;
	long	x, y, z;
	DWORD	dwVID;
	DWORD	dwVnum;
} TPacketGCItemGroundAdd;

typedef struct packet_ground_del_item
{
	BYTE	header;
	DWORD	vid;
} TPacketGCItemGroundDel;

typedef struct command_on_click
{
	BYTE	header;
	DWORD	vid;
} TPacketCGOnClick;

typedef struct packet_script
{
	BYTE	header;
	WORD	size;
	BYTE	skin;
	WORD	src_size;
} TPacketGCScript;

typedef struct command_script_answer
{
	BYTE	header;
	BYTE	answer;
} TPacketCGScriptAnswer;

#ifdef ENABLE_FAKE_FEATURE
typedef struct should_not_exist { BYTE header; } TPacketShouldNotExist;
#else
typedef struct packet_ping { BYTE header; } TPacketGCPing;
#endif

#pragma pack()
#endif
