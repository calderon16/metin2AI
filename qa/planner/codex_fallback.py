"""Gemini günlük kotası bittiğinde izole Codex CLI karar sağlayıcısı."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .llm import LLMError, LLMProvider, LLMReply, Message, QuotaExhausted, ToolCall, ToolSpec


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "tool_calls": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "args_json": {"type": "string"}}, "required": ["name", "args_json"],
            "additionalProperties": False}},
    },
    "required": ["text", "tool_calls"],
    "additionalProperties": False,
}


class CodexCliProvider:
    """Yalnız araç çağrısı önerir; oyun eylemleri mevcut QA yürütücüsünde çalışır."""

    name = "codex_cli"
    model = "configured"

    def __init__(self, executable: str = "codex", timeout_s: int = 180):
        self.executable = executable
        self.timeout_s = timeout_s

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMReply:
        allowed = {tool.name: tool for tool in tools}
        prompt = json.dumps({
            "instructions": system,
            "task": "Sadece verilen QA araçlarından bir veya birkaçını öner. Parametreleri args_json alanında JSON nesnesi metni olarak ver. Shell, dosya, SQL veya ağ aracı kullanma.",
            "tools": [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools],
            "messages": [{"role": m.role, "text": m.text,
                          "tool_calls": [{"name": c.name, "args": c.args} for c in m.tool_calls],
                          "tool_results": [{"name": r.name, "content": r.content} for r in m.tool_results]}
                         for m in messages],
        }, ensure_ascii=False, default=str)
        # Codex'e oyun/parola/API ortam değişkenleri geçirilmez.
        env = {k: v for k, v in os.environ.items()
               if not any(s in k.upper() for s in ("GEMINI", "QA_ACCOUNT", "QA_PANEL", "PASSWORD", "TOKEN", "API_KEY"))}
        with tempfile.TemporaryDirectory(prefix="mt2-codex-qa-") as directory:
            root = Path(directory)
            schema, output = root / "response.schema.json", root / "response.json"
            schema.write_text(json.dumps(_RESPONSE_SCHEMA), encoding="utf-8")
            command = [self.executable, "exec", "--ephemeral", "--ignore-user-config",
                       "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(root),
                       "--output-schema", str(schema), "-o", str(output), "-"]
            try:
                result = subprocess.run(command, input=prompt, text=True, capture_output=True,
                                        timeout=self.timeout_s, cwd=str(root), env=env, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise LLMError(f"Codex CLI çalıştırılamadı: {exc}") from exc
            if result.returncode:
                detail = (result.stderr or result.stdout)[-500:]
                raise LLMError(f"Codex CLI başarısız (kod {result.returncode}): {detail}")
            try:
                data = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise LLMError("Codex CLI geçerli yapılandırılmış yanıt vermedi") from exc
        calls = []
        for raw in data.get("tool_calls", []):
            name = raw.get("name")
            if name not in allowed:
                raise LLMError(f"Codex izin verilmeyen QA aracı önerdi: {name}")
            try:
                args = json.loads(raw.get("args_json", ""))
            except (ValueError, TypeError) as exc:
                raise LLMError(f"Codex araç parametreleri geçersiz: {name}") from exc
            if not isinstance(args, dict):
                raise LLMError(f"Codex araç parametreleri geçersiz: {name}")
            spec = allowed[name].parameters
            unknown = set(args) - set(spec.get("properties", {}))
            missing = set(spec.get("required", [])) - set(args)
            if unknown or missing:
                raise LLMError(f"Codex araç parametreleri geçersiz: {name}")
            calls.append(ToolCall(name, args))
        return LLMReply(Message("assistant", str(data.get("text", "")), calls))


class QuotaFallbackProvider:
    """Yalnız günlük kota bitince sonraki turları Codex CLI'ya yönlendirir."""

    def __init__(self, primary: LLMProvider, secondary: LLMProvider):
        self.primary, self.secondary = primary, secondary
        self.active: LLMProvider = primary

    @property
    def name(self) -> str:
        return self.active.name

    @property
    def model(self) -> str:
        return self.active.model

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMReply:
        try:
            return self.active.chat(system, messages, tools)
        except QuotaExhausted:
            if self.active is self.secondary:
                raise
            self.active = self.secondary
            return self.active.chat(system, messages, tools)


def create_explorer_provider(config: Any, model_override: str | None = None) -> LLMProvider:
    """Keşif için yapılandırılmış birincil ve günlük-kota yedeğini kurar."""
    from .llm import make_provider

    primary = make_provider(config.provider, model_override or config.model,
                            api_key_env=config.api_key_env, temperature=config.temperature,
                            thinking_budget=config.thinking_budget)
    if config.fallback_provider == "codex_cli":
        return QuotaFallbackProvider(primary, CodexCliProvider())
    if config.fallback_provider:
        raise LLMError(f"Bilinmeyen yedek LLM sağlayıcısı: {config.fallback_provider}")
    return primary
