// Metin2 AI QA Client Bridge — bkz. QaBridge.h
// UserInterface projesine ekleyin. StdAfx.h'nizin adı farklıysa düzeltin.
#include "StdAfx.h"

#ifdef ENABLE_AI_QA_CLIENT

#include "QaBridge.h"

static const size_t QA_MAX_LINE = 1024 * 1024;
static const size_t QA_MAX_CHAT_LOG = 500;

CQaBridge::CQaBridge() : m_listen(INVALID_SOCKET), m_client(INVALID_SOCKET), m_chatSeq(0)
{
}

CQaBridge::~CQaBridge()
{
	Destroy();
}

bool CQaBridge::Initialize(unsigned short port)
{
	// WSAStartup istemcinin ağ katmanı (EterLib/NetDevice) tarafından zaten yapılmış olmalı
	m_listen = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (m_listen == INVALID_SOCKET)
	{
		TraceError("QA bridge: socket() failed %d", WSAGetLastError());
		return false;
	}

	BOOL reuse = TRUE;
	setsockopt(m_listen, SOL_SOCKET, SO_REUSEADDR, (const char*)&reuse, sizeof(reuse));

	sockaddr_in addr = {};
	addr.sin_family = AF_INET;
	addr.sin_port = htons(port);
	addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK); // sadece 127.0.0.1

	if (bind(m_listen, (sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR || listen(m_listen, 1) == SOCKET_ERROR)
	{
		TraceError("QA bridge: bind/listen %u failed %d", port, WSAGetLastError());
		closesocket(m_listen);
		m_listen = INVALID_SOCKET;
		return false;
	}

	u_long nonBlocking = 1;
	ioctlsocket(m_listen, FIONBIO, &nonBlocking);
	Tracef("QA bridge listening on 127.0.0.1:%u\n", port);
	return true;
}

void CQaBridge::Destroy()
{
	__CloseClient();
	if (m_listen != INVALID_SOCKET)
	{
		closesocket(m_listen);
		m_listen = INVALID_SOCKET;
	}
}

void CQaBridge::__CloseClient()
{
	if (m_client != INVALID_SOCKET)
	{
		closesocket(m_client);
		m_client = INVALID_SOCKET;
	}
	m_recvBuf.clear();
}

void CQaBridge::Process()
{
	if (m_listen == INVALID_SOCKET)
		return;

	__Accept();
	__Receive();
	__CallPython("OnUpdate", NULL); // bekleyen `wait` yanıtları, olay tespiti
}

void CQaBridge::__Accept()
{
	SOCKET s = accept(m_listen, NULL, NULL);
	if (s == INVALID_SOCKET)
		return;

	// Yeni orchestrator bağlantısı eskisinin yerini alır
	__CloseClient();
	u_long nonBlocking = 1;
	ioctlsocket(s, FIONBIO, &nonBlocking);
	BOOL noDelay = TRUE;
	setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char*)&noDelay, sizeof(noDelay));
	m_client = s;
	__CallPython("OnConnect", NULL);
}

void CQaBridge::__Receive()
{
	if (m_client == INVALID_SOCKET)
		return;

	char buf[8192];
	for (;;)
	{
		int n = recv(m_client, buf, sizeof(buf), 0);
		if (n > 0)
		{
			m_recvBuf.append(buf, n);
			if (m_recvBuf.size() > QA_MAX_LINE)
			{
				TraceError("QA bridge: line too long, dropping client");
				__CloseClient();
				return;
			}
			continue;
		}
		if (n == 0 || WSAGetLastError() != WSAEWOULDBLOCK)
		{
			__CloseClient();
			__CallPython("OnDisconnect", NULL);
			return;
		}
		break;
	}

	size_t pos;
	while ((pos = m_recvBuf.find('\n')) != std::string::npos)
	{
		std::string line = m_recvBuf.substr(0, pos);
		m_recvBuf.erase(0, pos + 1);
		if (!line.empty() && line[line.size() - 1] == '\r')
			line.erase(line.size() - 1);
		if (!line.empty())
			__DispatchLine(line);
	}
}

void CQaBridge::__DispatchLine(const std::string& line)
{
	__CallPython("OnLine", &line);
}

void CQaBridge::__CallPython(const char* func, const std::string* arg)
{
	PyObject* mod = PyImport_ImportModule("qa_bridge");
	if (!mod)
	{
		PyErr_Print();
		return;
	}
	PyObject* fn = PyObject_GetAttrString(mod, func);
	if (fn && PyCallable_Check(fn))
	{
		PyObject* res = arg ? PyObject_CallFunction(fn, "s#", arg->c_str(), (int)arg->size())
		                    : PyObject_CallObject(fn, NULL);
		if (!res)
			PyErr_Print();
		Py_XDECREF(res);
	}
	Py_XDECREF(fn);
	Py_DECREF(mod);
}

void CQaBridge::SendLine(const std::string& line)
{
	if (m_client == INVALID_SOCKET)
		return;

	std::string data = line;
	data.push_back('\n');
	const char* p = data.c_str();
	int left = (int)data.size();
	while (left > 0)
	{
		int n = send(m_client, p, left, 0);
		if (n == SOCKET_ERROR)
		{
			if (WSAGetLastError() == WSAEWOULDBLOCK)
			{
				Sleep(1); // yerel soket; nadiren olur
				continue;
			}
			__CloseClient();
			return;
		}
		p += n;
		left -= n;
	}
}

void CQaBridge::OnChat(int type, const char* text)
{
	m_chatLog.push_back(std::make_pair(++m_chatSeq, std::make_pair(type, std::string(text ? text : ""))));
	while (m_chatLog.size() > QA_MAX_CHAT_LOG)
		m_chatLog.pop_front();
}

#endif // ENABLE_AI_QA_CLIENT
