// Metin2 AI QA — sunucu olay günlüğü (bkz. qa_event.h)
// game/src altına ekleyin ve Makefile/CMakeLists'e qa_event.cpp + cmd_qa.cpp'yi ekleyin.
#include "stdafx.h"

#ifdef ENABLE_AI_QA_SERVER

#include "qa_event.h"
#include "char.h"

bool g_bQaMode = false;

static void json_escape(std::string& out, const std::string& in)
{
	for (size_t i = 0; i < in.size(); ++i)
	{
		unsigned char c = in[i];
		switch (c)
		{
			case '"':  out += "\\\""; break;
			case '\\': out += "\\\\"; break;
			case '\n': out += "\\n"; break;
			case '\r': out += "\\r"; break;
			case '\t': out += "\\t"; break;
			default:
				if (c < 0x20)
				{
					char buf[8];
					snprintf(buf, sizeof(buf), "\\u%04x", c);
					out += buf;
				}
				else
					out += (char)c; // cp1254/UTF-8 baytları olduğu gibi (orchestrator errors=replace ile okur)
		}
	}
}

CQaEventLog::CQaEventLog() : m_fp(NULL)
{
	m_fp = fopen("qa_events.jsonl", "a");
	if (!m_fp)
		sys_err("QA: qa_events.jsonl açılamadı");
}

CQaEventLog::~CQaEventLog()
{
	if (m_fp)
		fclose(m_fp);
}

void CQaEventLog::Write(const char* type, const char* name, LPCHARACTER ch, std::initializer_list<QaKV> data)
{
	if (!m_fp)
		return;

	static unsigned long long s_seq = 0;
	std::string line;
	line.reserve(256);
	line += "{\"seq\":" + std::to_string(++s_seq);
	line += ",\"t\":" + std::to_string((unsigned long long)get_dword_time());
	line += ",\"type\":\"";
	json_escape(line, type);
	line += "\",\"name\":\"";
	json_escape(line, name);
	line += "\"";
	if (ch)
	{
		line += ",\"pid\":" + std::to_string((unsigned long long)ch->GetPlayerID());
		line += ",\"player\":\"";
		json_escape(line, ch->GetName());
		line += "\"";
	}
	line += ",\"data\":{";
	bool first = true;
	for (const QaKV& kv : data)
	{
		if (!first)
			line += ",";
		first = false;
		line += "\"";
		json_escape(line, kv.key);
		line += "\":";
		if (kv.quoted)
		{
			line += "\"";
			json_escape(line, kv.value);
			line += "\"";
		}
		else
			line += kv.value;
	}
	line += "}}\n";
	fputs(line.c_str(), m_fp);
	fflush(m_fp); // orchestrator satırı hemen görebilsin
}

#endif // ENABLE_AI_QA_SERVER
