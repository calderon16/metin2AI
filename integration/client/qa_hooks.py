# -*- coding: utf-8 -*-
# Metin2 AI QA — UI kancaları (root/qa_hooks.py), Python 2.7
# ---------------------------------------------------------------------------
# INTEGRATION.md 1.4'teki kancaları kaynak fonksiyonların içine tek tek yazmak yerine, ilgili
# modülün sonunda sınıf metotlarını sarar. Her modülün sonuna yalnızca şu satırlar eklenir:
#
#     if getattr(app, "ENABLE_AI_QA_CLIENT", 0):
#         import qa_hooks
#         qa_hooks.Install(globals())
#
# Normal istemcide `app.ENABLE_AI_QA_CLIENT` yoktur (veya 0'dır); bu dosya hiç import edilmez.
# Sarmalayıcılar önce orijinal metodu çağırır; kanca hatası oyunu asla bozmaz (dbg'ye yazılır).

import qa_bridge


def _trace(message):
	try:
		import dbg
		dbg.TraceError("qa_hooks: " + message)
	except Exception:
		pass


def _wrap(cls, name, after=None, before=None):
	original = getattr(cls, name, None)
	if original is None or getattr(original, "_qa_wrapped", False):
		return

	def wrapper(self, *args):
		if before:
			try:
				before(self, *args)
			except Exception, e:
				_trace("%s.%s before: %s" % (cls.__name__, name, e))
		result = original(self, *args)
		if after:
			try:
				after(self, *args)
			except Exception, e:
				_trace("%s.%s after: %s" % (cls.__name__, name, e))
		return result

	wrapper._qa_wrapped = True
	setattr(cls, name, wrapper)


def _install_login(cls):
	_wrap(cls, "__init__", after=lambda self, *a: qa_bridge.RegisterStage("login", self))
	_wrap(cls, "Close", before=lambda self, *a: qa_bridge.UnregisterStage("login", self))
	_wrap(cls, "OnLoginFailure", before=lambda self, error, *a: qa_bridge.OnLoginFailure(error))


def _install_select(cls):
	_wrap(cls, "Open", after=lambda self, *a: qa_bridge.RegisterStage("select", self))
	_wrap(cls, "Close", before=lambda self, *a: qa_bridge.UnregisterStage("select", self))


def _install_game(cls):
	_wrap(cls, "Open", after=lambda self, *a: qa_bridge.OnEnterGame())
	_wrap(cls, "Close", before=lambda self, *a: qa_bridge.OnLeaveGame())
	_wrap(cls, "StartShop", after=lambda self, vid, *a: qa_bridge.OnShopOpen(vid))
	_wrap(cls, "EndShop", after=lambda self, *a: qa_bridge.OnShopClose())
	_wrap(cls, "StartExchange", after=lambda self, *a: qa_bridge.OnTradeStart())
	_wrap(cls, "EndExchange", after=lambda self, *a: qa_bridge.OnTradeEnd())
	_wrap(cls, "RecvPartyInviteQuestion", before=lambda self, vid, name, *a: qa_bridge.OnPartyInvite(vid, name))
	_wrap(cls, "AddPartyMember", after=lambda self, pid, name, *a: qa_bridge.OnPartyMember(pid, name))
	_wrap(cls, "RemovePartyMember", after=lambda self, pid, *a: qa_bridge.OnPartyMemberRemoved(pid))
	_wrap(cls, "ExitParty", after=lambda self, *a: qa_bridge.OnPartyExit())


def _install_quest(cls):
	def appended(self, name, idx):
		options = getattr(self, "_qa_options", None)
		if options is None:
			options = self._qa_options = {}
		options[idx] = name
		ordered = [options[i] for i in sorted(options.keys())]
		# Seçim, butonun SetEvent ile bağlandığı ClickAnswerEvent'in aynısıdır
		qa_bridge.OnQuestDialog("", ordered, lambda i, s=self: s.ClickAnswerEvent(i), self.CloseSelf)

	def closed(self, *a):
		self._qa_options = None
		qa_bridge.OnQuestDialogClosed()

	_wrap(cls, "AppendQuestion", after=appended)
	_wrap(cls, "CloseSelf", before=closed)


_INSTALLERS = (
	("LoginWindow", _install_login),
	("SelectCharacterWindow", _install_select),
	("GameWindow", _install_game),
	("QuestDialog", _install_quest),
)


def Install(namespace):
	for class_name, installer in _INSTALLERS:
		cls = namespace.get(class_name)
		if cls is not None:
			try:
				installer(cls)
			except Exception, e:
				_trace("%s: %s" % (class_name, e))
