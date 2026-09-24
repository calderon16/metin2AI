#pragma once
// Metin2 AI QA Client Bridge
// ---------------------------------------------------------------------------
// Yalnızca QA build'inde derlenir (Locale_inc.h / UserInterface.h içinde):
//     #define ENABLE_AI_QA_CLIENT
//
// Görev: 127.0.0.1:<port> üzerinde tek bir orchestrator bağlantısı kabul eder, satır başına bir
// JSON mesajı alır ve her satırı Python tarafındaki `qa_bridge.OnLine(line)` fonksiyonuna iletir.
// JSON işleme, durum okuma ve aksiyonlar Python'da (root/qa_bridge.py) yapılır; yanıtlar
// `qa.Send(line)` ile geri yollanır.
//
// Her şey ana thread'de, CPythonApplication::Process() içinden çağrılan Process() ile olur:
// thread yok, kilit yok, oyun durumuna erişim güvenli.
//
// Güvenlik: soket sadece loopback'e bind edilir ve bu kod QA dışı build'lere hiç girmez.

#ifdef ENABLE_AI_QA_CLIENT

#include <string>
#include <deque>
#include <winsock2.h>

class CQaBridge : public CSingleton<CQaBridge>
{
public:
	CQaBridge();
	virtual ~CQaBridge();

	bool Initialize(unsigned short port);
	void Destroy();

	// Her frame çağrılır: bağlantı kabul et, satırları oku, Python'a ilet, qa_bridge.OnUpdate() çağır
	void Process();

	// Python'dan (qa.Send) çağrılır
	void SendLine(const std::string& line);

	// PythonChat.cpp AppendChat içinden çağrılır — sistem mesajlarını orchestrator'a açar
	void OnChat(int type, const char* text);
	const std::deque<std::pair<unsigned int, std::pair<int, std::string>>>& GetChatLog() const { return m_chatLog; }

	bool IsConnected() const { return m_client != INVALID_SOCKET; }

private:
	void __Accept();
	void __Receive();
	void __CloseClient();
	void __DispatchLine(const std::string& line);
	void __CallPython(const char* func, const std::string* arg);

private:
	SOCKET m_listen;
	SOCKET m_client;
	std::string m_recvBuf;
	std::deque<std::pair<unsigned int, std::pair<int, std::string>>> m_chatLog;
	unsigned int m_chatSeq;
};

#endif // ENABLE_AI_QA_CLIENT
