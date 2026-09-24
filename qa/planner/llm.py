"""LLM sağlayıcı arayüzü (modelden bağımsız) + Gemini ve senaryolu (test) sağlayıcılar.

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
                 max_retries: int = 4, opener: Callable[..., Any] | None = None):
        key = api_key or os.environ.get(api_key_env) or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise LLMError(f"Gemini API anahtarı yok: {api_key_env} ortam değişkenini ayarlayın")
        self.model = model
        self._key = key
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._open = opener or urllib.request.urlopen

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
                detail = e.read().decode("utf-8", errors="replace")[:1000]
                if e.code in _RETRY_STATUS and attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"Gemini HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < self.max_retries:
                    time.sleep(delay)
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
        usage = {"input_tokens": u.get("promptTokenCount", 0), "output_tokens": u.get("candidatesTokenCount", 0),
                 "total_tokens": u.get("totalTokenCount", 0)}
        return LLMReply(Message("assistant", "\n".join(text), calls, raw=content), usage, cand.get("finishReason"))


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

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMReply:
        self.seen.append(list(messages))
        if callable(self.script):
            calls = self.script(messages)
        elif self.turn < len(self.script):
            item = self.script[self.turn]
            items = item if isinstance(item, list) else [item]
            calls = [ToolCall(c["name"], c.get("args", {}), f"s{self.turn}_{i}") for i, c in enumerate(items)]
        else:
            calls = []
        self.turn += 1
        return LLMReply(Message("assistant", "", calls), {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})


def make_provider(provider: str, model: str | None = None, **kw: Any) -> LLMProvider:
    if provider == "gemini":
        return GeminiProvider(model or os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash", **kw)
    raise LLMError(f"Bilinmeyen LLM sağlayıcı: {provider} (mevcut: gemini)")
