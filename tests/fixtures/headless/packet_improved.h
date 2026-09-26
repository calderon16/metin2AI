// Test fixture'ı: _IMPROVED_PACKET_ENCRYPTION_ paketleri ve ayrıştırıcının zorlandığı yapılar
// (struct içi static const, metot gövdeli struct, adlı enum tipli alan, "*PTR" takma adlı typedef).
enum
{
	HEADER_CG_KEY_AGREEMENT = 0xfb,
	HEADER_GC_KEY_AGREEMENT_COMPLETED = 0xfa,
	HEADER_GC_KEY_AGREEMENT = 0xfb,
	HEADER_GC_WARP = 41,
	HEADER_CG_EXCHANGE = 50,
	HEADER_GC_EXCHANGE = 42,
};

enum EFixtureKind { KIND_A, KIND_B };

#pragma pack(1)
struct TPacketKeyAgreement
{
	static const int MAX_DATA_LEN = 256;
	BYTE bHeader;
	WORD wAgreedLength;
	WORD wDataLength;
	BYTE data[MAX_DATA_LEN];
};

typedef struct packet_warp
{
	BYTE	bHeader;
	long	lX;
	long	lY;
	long	lAddr;
	WORD	wPort;
} TPacketGCWarp;

typedef struct command_exchange
{
	BYTE		header;
	BYTE		subheader;
	DWORD		arg1;
	BYTE		arg2;
	TItemPos	Pos;
} TPacketCGExchange;

typedef struct packet_exchange
{
	BYTE		header;
	BYTE		subheader;
	BYTE		is_me;
	DWORD		arg1;
	TItemPos	arg2;
	DWORD		arg3;
	long		alValues[3];
} TPacketGCExchange;

struct TPacketKeyAgreementCompleted
{
	BYTE bHeader;
	BYTE data[3]; // dummy (not used)
};

typedef struct SFixturePos
{
	BYTE window_type;
	WORD cell;
	SFixturePos() { window_type = 1; cell = 0xffff; }
	bool IsValid() const
	{
		switch (window_type)
		{
		case 1: return cell < 90;
		}
		return false;
	}
} TFixturePos;

typedef struct SFixtureMixed
{
	enum { NAME_LEN = 4 };
	EFixtureKind eKind;
	TFixturePos pos;
	char szName[NAME_LEN + 1];
} TFixtureMixed, *PFixtureMixed;
