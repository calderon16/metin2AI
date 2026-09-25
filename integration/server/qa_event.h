#pragma once
// Metin2 AI QA — sunucu enstrümantasyonu
// ---------------------------------------------------------------------------
// Yalnızca QA build'inde (service.h / CommonDefines.h):
//     #define ENABLE_AI_QA_SERVER
// ve CONFIG'de `QA_MODE: 1` iken aktiftir. Olaylar game çekirdeğinin klasörüne
// `qa_events.jsonl` olarak (satır başına bir JSON) yazılır; orchestrator bunu
// qa.toml [server].events_file ile okur.
//
// Kullanım:
//     QA_EVENT("ITEM_UPGRADE", ch, QA_KV("vnum", item->GetVnum()), QA_KV("result", bSuccess));
//     QA_ASSERT(ch->GetGold() >= 0, "PLAYER_NEGATIVE_GOLD", ch, QA_KV("gold", ch->GetGold()));
//     QA_ERROR("QUEST_ERROR", ch, QA_KV("quest", szQuestName));
//
// QA build'i dışında tüm makrolar boştur (sıfır maliyet).

#ifdef ENABLE_AI_QA_SERVER

#include <string>
#include <initializer_list>
#include <cstdio>

class CHARACTER;
typedef CHARACTER* LPCHARACTER;

extern bool g_bQaMode;

// QA_MODE açıkken "AI_QA_" önekli hesap/karakter adlarında "_" karakterine izin verilir
// (check_name_alphabet ve FN_IS_VALID_LOGIN_STRING yalnızca harf/rakam kabul eder).
bool QaAllowsReservedName(const char* name);

struct QaKV
{
	const char* key;
	std::string value;
	bool quoted;

	QaKV(const char* k, const char* v) : key(k), value(v ? v : ""), quoted(true) {}
	QaKV(const char* k, const std::string& v) : key(k), value(v), quoted(true) {}
	QaKV(const char* k, bool v) : key(k), value(v ? "true" : "false"), quoted(false) {}
	template <typename T>
	QaKV(const char* k, T v) : key(k), value(std::to_string(static_cast<long long>(v))), quoted(false) {}
};

class CQaEventLog : public singleton<CQaEventLog>
{
public:
	CQaEventLog();
	~CQaEventLog();

	// type: "event" | "assert_fail" | "syserr" | "quest_error" | "sql_error"
	void Write(const char* type, const char* name, LPCHARACTER ch, std::initializer_list<QaKV> data);

private:
	FILE* m_fp;
};

// Tembel oluşturma: singleton ilk olayda yaratılır (main.cpp değişikliği gerekmez).
CQaEventLog& QaEventLog();

#define QA_KV(k, v) QaKV((k), (v))
#define QA_EVENT(name, ch, ...) \
	do { if (g_bQaMode) QaEventLog().Write("event", (name), (ch), { __VA_ARGS__ }); } while (0)
#define QA_ERROR(name, ch, ...) \
	do { if (g_bQaMode) QaEventLog().Write("quest_error", (name), (ch), { __VA_ARGS__ }); } while (0)
#define QA_ASSERT(cond, name, ch, ...) \
	do { if (g_bQaMode && !(cond)) QaEventLog().Write("assert_fail", (name), (ch), \
		{ QaKV("cond", #cond), ##__VA_ARGS__ }); } while (0)

#else

#define QA_KV(k, v)
#define QA_EVENT(name, ch, ...) do {} while (0)
#define QA_ERROR(name, ch, ...) do {} while (0)
#define QA_ASSERT(cond, name, ch, ...) do {} while (0)

#endif // ENABLE_AI_QA_SERVER
