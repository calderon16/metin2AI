# -*- coding: utf-8 -*-
# Metin2 AI QA — köprü için bağımsız JSON (root/qa_json.py), Python 2.7
# ---------------------------------------------------------------------------
# İstemcinin gömülü Python'unda stdlib `json` paketi çalışmayabilir: `_struct` yerleşik modülü
# yoktur ve system.py'deki içe aktarıcı paket/göreli içe aktarmayı desteklemez. Bu modül yalnız
# yerleşik türleri kullanır (re, struct, codecs gerekmez).
#
# Oyun metinleri CP1254 bayt dizileridir; dumps bunları Unicode'a çevirip \uXXXX olarak yazar.
# loads yalnız ASCII içeren metinleri `str`, diğerlerini `unicode` döndürür.

# CP1254'ün latin-1'den farklı olan kod noktaları (0x80-0x9F ve Türkçe harfler)
_CP1254 = {
	0x80: 0x20AC, 0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E, 0x85: 0x2026, 0x86: 0x2020, 0x87: 0x2021,
	0x88: 0x02C6, 0x89: 0x2030, 0x8A: 0x0160, 0x8B: 0x2039, 0x8C: 0x0152, 0x91: 0x2018, 0x92: 0x2019,
	0x93: 0x201C, 0x94: 0x201D, 0x95: 0x2022, 0x96: 0x2013, 0x97: 0x2014, 0x98: 0x02DC, 0x99: 0x2122,
	0x9A: 0x0161, 0x9B: 0x203A, 0x9C: 0x0153, 0x9F: 0x0178,
	0xD0: 0x011E, 0xDD: 0x0130, 0xDE: 0x015E, 0xF0: 0x011F, 0xFD: 0x0131, 0xFE: 0x015F,
}

_ESCAPES = {'"': '\\"', '\\': '\\\\', '\n': '\\n', '\r': '\\r', '\t': '\\t', '\b': '\\b', '\f': '\\f'}


def _bytes_to_codepoints(s):
	try:
		return [ord(ch) for ch in s.decode("utf-8")]
	except Exception:
		return [_CP1254.get(ord(ch), ord(ch)) for ch in s]


def _quote(s):
	if isinstance(s, unicode):
		points = [ord(ch) for ch in s]
	else:
		points = _bytes_to_codepoints(s)
	out = ['"']
	for cp in points:
		ch = unichr(cp) if cp > 127 else chr(cp)
		if ch in _ESCAPES:
			out.append(_ESCAPES[ch])
		elif cp < 0x20 or cp > 0x7E:
			if cp > 0xFFFF:
				cp -= 0x10000
				out.append("\\u%04x\\u%04x" % (0xD800 | (cp >> 10), 0xDC00 | (cp & 0x3FF)))
			else:
				out.append("\\u%04x" % cp)
		else:
			out.append(ch)
	out.append('"')
	return "".join(out)


def dumps(obj):
	if obj is None:
		return "null"
	if obj is True:
		return "true"
	if obj is False:
		return "false"
	if isinstance(obj, (int, long)):
		return str(obj)
	if isinstance(obj, float):
		if obj != obj or obj in (float("inf"), float("-inf")):
			return "null"
		return repr(obj)
	if isinstance(obj, basestring):
		return _quote(obj)
	if isinstance(obj, dict):
		return "{" + ", ".join([_quote(k if isinstance(k, basestring) else str(k)) + ": " + dumps(v) for k, v in obj.items()]) + "}"
	if isinstance(obj, (list, tuple)):
		return "[" + ", ".join([dumps(v) for v in obj]) + "]"
	raise TypeError("qa_json: desteklenmeyen tür %r" % type(obj))


class _Parser(object):
	def __init__(self, text):
		if isinstance(text, str):
			try:
				text = text.decode("utf-8")
			except Exception:
				text = u"".join([unichr(_CP1254.get(ord(ch), ord(ch))) for ch in text])
		self.s = text
		self.i = 0

	def fail(self, message):
		raise ValueError("qa_json: %s (konum %d)" % (message, self.i))

	def ws(self):
		s, n = self.s, len(self.s)
		while self.i < n and s[self.i] in u" \t\r\n":
			self.i += 1

	def value(self):
		self.ws()
		if self.i >= len(self.s):
			self.fail("beklenmeyen son")
		ch = self.s[self.i]
		if ch == u"{":
			return self.obj()
		if ch == u"[":
			return self.arr()
		if ch == u'"':
			return self.string()
		for word, result in ((u"true", True), (u"false", False), (u"null", None)):
			if self.s.startswith(word, self.i):
				self.i += len(word)
				return result
		return self.number()

	def obj(self):
		self.i += 1
		out = {}
		self.ws()
		if self.s[self.i:self.i + 1] == u"}":
			self.i += 1
			return out
		while True:
			self.ws()
			if self.s[self.i:self.i + 1] != u'"':
				self.fail("anahtar bekleniyordu")
			key = self.string()
			self.ws()
			if self.s[self.i:self.i + 1] != u":":
				self.fail("':' bekleniyordu")
			self.i += 1
			out[key] = self.value()
			self.ws()
			ch = self.s[self.i:self.i + 1]
			self.i += 1
			if ch == u"}":
				return out
			if ch != u",":
				self.fail("',' veya '}' bekleniyordu")

	def arr(self):
		self.i += 1
		out = []
		self.ws()
		if self.s[self.i:self.i + 1] == u"]":
			self.i += 1
			return out
		while True:
			out.append(self.value())
			self.ws()
			ch = self.s[self.i:self.i + 1]
			self.i += 1
			if ch == u"]":
				return out
			if ch != u",":
				self.fail("',' veya ']' bekleniyordu")

	def string(self):
		self.i += 1
		s, out = self.s, []
		while True:
			if self.i >= len(s):
				self.fail("kapanmamış metin")
			ch = s[self.i]
			self.i += 1
			if ch == u'"':
				break
			if ch != u"\\":
				out.append(ch)
				continue
			esc = s[self.i:self.i + 1]
			self.i += 1
			if esc == u"u":
				cp = int(s[self.i:self.i + 4], 16)
				self.i += 4
				if 0xD800 <= cp < 0xDC00 and s[self.i:self.i + 2] == u"\\u":
					low = int(s[self.i + 2:self.i + 6], 16)
					self.i += 6
					cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)
				try:
					out.append(unichr(cp))
				except ValueError:  # dar (UCS-2) derleme: vekil çifti olarak bırak
					out.append(unichr(0xD800 + ((cp - 0x10000) >> 10)) + unichr(0xDC00 + ((cp - 0x10000) & 0x3FF)))
			else:
				out.append({u'"': u'"', u"\\": u"\\", u"/": u"/", u"b": u"\b", u"f": u"\f",
				            u"n": u"\n", u"r": u"\r", u"t": u"\t"}.get(esc, esc))
		text = u"".join(out)
		try:
			return str(text)  # yalnız ASCII ise oyun modüllerine doğrudan verilebilir
		except UnicodeError:
			return text

	def number(self):
		start = self.i
		s, n = self.s, len(self.s)
		while self.i < n and s[self.i] in u"+-0123456789.eE":
			self.i += 1
		token = s[start:self.i]
		if not token:
			self.fail("değer bekleniyordu")
		if u"." in token or u"e" in token or u"E" in token:
			return float(token)
		return int(token)


def loads(text):
	parser = _Parser(text)
	result = parser.value()
	parser.ws()
	if parser.i != len(parser.s):
		parser.fail("fazladan veri")
	return result


_CP1254_REVERSE = dict((v, k) for k, v in _CP1254.items())


def encode_cp1254(text):
	"""Unicode -> oyunun CP1254 baytları (istemcide cp1254 codec'i yok). Karşılığı olmayan '?' olur."""
	if not isinstance(text, unicode):
		return text
	out = []
	for ch in text:
		cp = ord(ch)
		if cp in _CP1254_REVERSE:
			out.append(chr(_CP1254_REVERSE[cp]))
		elif cp < 256 and cp not in _CP1254:
			out.append(chr(cp))
		else:
			out.append("?")
	return "".join(out)
