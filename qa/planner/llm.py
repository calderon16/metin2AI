"""LLM sağlayıcı arayüzü (modelden bağımsız) + Gemini, yerel Ollama ve senaryolu (test) sağlayıcılar.

Otonom keşif ajanı yalnızca bu arayüzü bilir; Gemini dışında bir model (Claude vb.) eklemek
için `LLMProvider.chat`'i uygulayan yeni bir sınıf yeterlidir.

Gemini, `google-genai` SDK'sı yerine doğrudan REST API ile çağrılır (ek bağımlılık yok):
    POST {base_url}/models/{model}:generateContent   (başlık: x-goog-api-key)
API anahtarı asla dosyaya yazılmaz; ortam değişkeninden okunur (varsayılan GEMINI_API_KEY).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class LLMError(Exception):
    pass


class QuotaExhausted(LLMError):
    """Sağlayıcının günlük kotası doldu; yedek sağlayıcıya geçilebilir."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]           # JSON Schema (object)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass
class ToolResult:
    name: str
    content: dict[str, Any]
    id: str = ""


@dataclass
class Message:
    role: str                            # "user" | "assistant" | "tool"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    # Sağlayıcının ham içeriği (ör. Gemini thoughtSignature'ları korunarak geri gönderilir)
    raw: Any = None


@dataclass
class LLMReply:
    message: Message
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMReply: ...


# ---------------------------------------------------------------------- Gemini

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_WAIT_S = 90.0


def _quota_info(detail: str) -> tuple[float | None, bool]:
    """429 gövdesinden (google.rpc RetryInfo / QuotaFailure) bekleme süresi ve günlük kota olup olmadığı."""
    try:
        details = json.loads(detail).get("error", {}).get("details", [])
    except (ValueError, AttributeError):
        details = []
    wait, daily = None, False
    for d in details if isinstance(details, list) else []:
        t = str(d.get("@type", ""))
        if t.endswith("RetryInfo"):
            try:
                wait = float(str(d.get("retryDelay", "")).rstrip("s"))
            except ValueError:
                pass
        if t.endswith("QuotaFailure"):
            daily = daily or any("PerDay" in str(v.get("quotaId", "")) for v in d.get("violations", []))
    return wait, daily


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema'yı Gemini'nin desteklediği OpenAPI alt kümesine indirger."""
    out: dict[str, Any] = {}
    for k in ("type", "description", "enum", "format", "nullable"):
        if k in schema:
            out[k] = schema[k]
    if "properties" in schema:
        out["properties"] = {k: _gemini_schema(v) for k, v in schema["properties"].items()}
    if schema.get("required"):
        out["required"] = list(schema["required"])
    if "items" in schema:
        out["items"] = _gemini_schema(schema["items"])
    return out


class GeminiProvider:
    name = "gemini"

    def __init__(self, model: str, api_key: str | None = None, api_key_env: str = "GEMINI_API_KEY",
                 base_url: str = GEMINI_BASE_URL, temperature: float = 0.4, timeout_s: float = 120.0,
                 max_retries: int = 4, opener: Callable[..., Any] | None = None,
                 thinking_budget: int | None = None, sleep: Callable[[float], None] = time.sleep):
        key = api_key or os.environ.get(api_key_env) or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise LLMError(f"Gemini API anahtarı yok: {api_key_env} ortam değişkenini ayarlayın")
        self.model = model
        self._key = key
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.thinking_budget = thinking_budget
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    # -- dönüştürme
    @staticmethod
    def _content(m: Message) -> dict[str, Any]:
        if m.role == "assistant":
            if m.raw is not None:
                return m.raw                  # thoughtSignature vb. aynen geri gider
            parts: list[dict[str, Any]] = []
            if m.text:
                parts.append({"text": m.text})
            parts += [{"functionCall": {"name": c.name, "args": c.args}} for c in m.tool_calls]
            return {"role": "model", "parts": parts}
        parts = [{"functionResponse": {"name": r.name, "response": r.content, **({"id": r.id} if r.id else {})}}
                 for r in m.tool_results]
        if m.text:
            parts.append({"text": m.text})
        return {"role": "user", "parts": parts}

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/models/{self.model}:generateContent"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": self._key})
            try:
                with self._open(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:4000]
                if e.code == 429:
                    wait, daily = _quota_info(detail)
                    if daily:
                        raise QuotaExhausted(f"Gemini günlük kotası doldu ({self.model}). Yarın sıfırlanır; hemen devam "
                                       "etmek için Google AI Studio'da faturalandırmayı açın ya da daha yüksek ücretsiz "
                                       "sınırı olan bir model seçin ([explorer] model).") from e
                    if attempt < self.max_retries:
                        self._sleep(min(_MAX_RETRY_WAIT_S, (wait if wait is not None else delay) + 1.0))
                        delay *= 2
                        continue
                    raise LLMError(f"Gemini dakikalık kotası aşıldı ({self.model}); [explorer] "
                                   "requests_per_minute değerini azaltın. Ayrıntı: " + detail[:300]) from e
                if e.code in _RETRY_STATUS and attempt < self.max_retries:
                    self._sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"Gemini HTTP {e.code}: {detail[:1000]}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < self.max_retries:
                    self._sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"Gemini'ye ulaşılamadı: {e}") from e
        raise LLMError("Gemini: yeniden deneme hakkı bitti")

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMReply:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [self._content(m) for m in messages],
            "tools": [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": _gemini_schema(t.parameters)}
                for t in tools]}],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": {"temperature": self.temperature},
        }
        if self.thinking_budget is not None:
            # "Düşünme" token'ları çıktı olarak faturalanır; sınırlamak maliyeti düşürür (0 = kapalı)
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": int(self.thinking_budget)}
        data = self._request(body)
        cands = data.get("candidates") or []
        if not cands:
            fb = data.get("promptFeedback", {})
            raise LLMError(f"Gemini yanıt üretmedi: {fb or data}")
        cand = cands[0]
        content = cand.get("content") or {"role": "model", "parts": []}
        content.setdefault("role", "model")
        text, calls = [], []
        for i, part in enumerate(content.get("parts", [])):
            if "text" in part and not part.get("thought"):
                text.append(part["text"])
            if "functionCall" in part:
                fc = part["functionCall"]
                calls.append(ToolCall(fc.get("name", ""), fc.get("args") or {}, fc.get("id", "")))
        u = data.get("usageMetadata", {})
        # Düşünme token'ları çıktı gibi faturalanır; önbellekten okunan girdi token'ları daha ucuzdur
        usage = {"input_tokens": u.get("promptTokenCount", 0),
                 "output_tokens": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
                 "cached_tokens": u.get("cachedContentTokenCount", 0),
                 "total_tokens": u.get("totalTokenCount", 0)}
        return LLMReply(Message("assistant", "\n".join(text), calls, raw=content), usage, cand.get("finishReason"))


# ---------------------------------------------------------------------- Ollama (yerel model)

OLLAMA_URL = "http://127.0.0.1:11434"


def ollama_available(base_url: str = OLLAMA_URL, timeout_s: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=timeout_s) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _json_tool_calls(text: str, tools: list[ToolSpec]) -> list[ToolCall]:
    """Küçük modeller komutu bazen metin içinde JSON olarak yazar: {"name": ..., "arguments": {...}}."""
    names = {t.name for t in tools}
    start = text.find("{")
    while start != -1:
        depth = 0
        for end in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            if depth == 0:
                try:
                    obj = json.loads(text[start:end + 1])
                except ValueError:
                    break
                items = obj if isinstance(obj, list) else [obj]
                calls = [ToolCall(o.get("name", ""), o.get("arguments") or o.get("args") or o.get("parameters") or {})
                         for o in items if isinstance(o, dict) and o.get("name") in names]
                if calls:
                    return calls
                break
        start = text.find("{", start + 1)
    return []


class OllamaProvider:
    """Yerel Ollama sunucusu (http://127.0.0.1:11434) — kota ve ücret yok; metin2 için ince ayarlı modeller
    (bkz. training/) `ollama create` ile eklenip [explorer] model ile seçilir."""

    name = "ollama"

    def __init__(self, model: str, base_url: str = OLLAMA_URL, temperature: float = 0.4, timeout_s: float = 300.0,
                 num_ctx: int = 8192, max_retries: int = 2, opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] | None = None, think: bool | None = None, **_ignored: Any):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx
        self.max_retries = max_retries
        self._open = opener or urllib.request.urlopen
        self._sleep_fn = sleep
        # Düşünen modeller (qwen3, deepseek-r1): False = her adımda uzun akıl yürütme üretme (çok daha hızlı)
        self.think = think

    @staticmethod
    def _messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.text or ""}
                if m.tool_calls:
                    msg["tool_calls"] = [{"function": {"name": c.name, "arguments": c.args}} for c in m.tool_calls]
                out.append(msg)
                continue
            for r in m.tool_results:
                out.append({"role": "tool", "tool_name": r.name, "content": json.dumps(r.content, ensure_ascii=False)})
            if m.text:
                out.append({"role": "user", "content": m.text})
        return out

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(f"{self.base_url}/api/chat", data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with self._open(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:500]
                if e.code == 404 and "not found" in detail:
                    raise LLMError(f"Ollama'da '{self.model}' modeli yok: `ollama pull {self.model}`") from e
                if e.code >= 500 and attempt < self.max_retries:
                    (self._sleep_fn or time.sleep)(2.0 * (attempt + 1))
                    continue
                raise LLMError(f"Ollama HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < self.max_retries:
                    (self._sleep_fn or time.sleep)(2.0 * (attempt + 1))
                    continue
                raise LLMError(f"Ollama'ya ulaşılamadı ({self.base_url}): {e}. Ollama çalışıyor mu?") from e
        raise LLMError("Ollama: yeniden deneme hakkı bitti")

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMReply:
        body = {
            "model": self.model,
            "messages": self._messages(system, messages),
            "tools": [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                        "parameters": t.parameters}} for t in tools],
            "stream": False,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        if self.think is not None:
            body["think"] = self.think
        data = self._post(body)
        msg = data.get("message") or {}
        text = msg.get("content") or ""
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(fn.get("name", ""), args, f"o{i}"))
        if not calls and tools:
            calls = _json_tool_calls(text, tools)
        pin, pout = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
        usage = {"input_tokens": pin, "output_tokens": pout, "cached_tokens": 0, "total_tokens": pin + pout}
        return LLMReply(Message("assistant", text, calls), usage, data.get("done_reason"))


# ---------------------------------------------------------------------- senaryolu (testler / demo)

class ScriptedProvider:
    """Önceden yazılmış tool çağrılarını sırayla döndürür — API olmadan döngüyü test etmek için.

    script: her öğe bir tur; öğe ya tek bir {"name", "args"} ya da bunların listesi.
    Callable da verilebilir: fn(messages) -> list[ToolCall] (önceki sonuçlara göre karar vermek için).
    """

    name = "scripted"
    model = "scripted"

    def __init__(self, script: list[Any] | Callable[[list[Message]], list[ToolCall]]):
        self.script = script
        self.turn = 0
        self.seen: list[list[Message]] = []
        # Testlerde bütçe/maliyet hesabını sınamak için her çağrının "kullanımı"
        self.usage_per_call = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0}

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMReply:
        self.seen.append(list(messages))
        text = ""
        if callable(self.script):
            calls = self.script(messages)
            if isinstance(calls, str):
                text, calls = calls, []
        elif self.turn < len(self.script):
            item = self.script[self.turn]
            if isinstance(item, dict) and "text" in item:
                text, calls = str(item["text"]), []
            else:
                items = item if isinstance(item, list) else [item]
                calls = [ToolCall(c["name"], c.get("args", {}), f"s{self.turn}_{i}") for i, c in enumerate(items)]
        else:
            calls = []
        self.turn += 1
        return LLMReply(Message("assistant", text, calls), dict(self.usage_per_call))


def make_provider(provider: str, model: str | None = None, **kw: Any) -> LLMProvider:
    if provider == "gemini":
        # Varsayılan: en ucuz ve ücretsiz katmanda bulunan model
        return GeminiProvider(model or os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash-lite", **kw)
    if provider == "ollama":
        kw.pop("api_key_env", None)
        kw.pop("thinking_budget", None)
        return OllamaProvider(model or os.environ.get("OLLAMA_MODEL") or "qwen2.5:7b", **kw)
    raise LLMError(f"Bilinmeyen LLM sağlayıcı: {provider} (mevcut: gemini, ollama)")
