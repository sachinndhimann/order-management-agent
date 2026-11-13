"""LLM client abstraction (Chapter 7).

The agent depends on the `LLMClient` Protocol, not on a vendor. Production uses
any OpenAI-compatible /chat/completions endpoint (Groq, OpenAI, Ollama, vLLM);
tests use ScriptedLLM, so the full agent loop runs deterministically with no
network and no API cost. Reusing BaseHttpConnector gives the LLM call the same
timeout / retry / breaker behaviour as every other outside dependency.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.connectors.base import BaseHttpConnector, BearerAuth, ConnectorError, RetryPolicy
from app.core.config import Settings
from app.core.errors import UpstreamUnavailableError


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantTurn:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> AssistantTurn: ...
    async def aclose(self) -> None: ...


class OpenAICompatibleLLM(BaseHttpConnector):
    name = "llm"

    def __init__(self, settings: Settings, **kw):
        super().__init__(settings.llm_base_url, auth=BearerAuth(settings.llm_api_key.get_secret_value()),
                         timeout=60.0, retry=RetryPolicy(2, 1.0), **kw)
        self.model = settings.llm_model

    async def complete(self, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"  # the MODEL decides whether/which tool to call
        try:
            # Completions are not idempotent in cost terms, but repeating one is harmless: allow retry.
            resp = await self.request("POST", "/chat/completions", json=payload, idempotent=True)
        except ConnectorError as exc:
            raise UpstreamUnavailableError(f"LLM unavailable: {exc}") from exc
        msg = resp.json()["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_json__": tc["function"].get("arguments")}
            calls.append(ToolCall(tc["id"], tc["function"]["name"], args))
        return AssistantTurn(msg.get("content"), calls)


class ScriptedLLM:
    """Deterministic fake. `script` is a list of AssistantTurn (consumed in order)
    or a function (messages, tools) -> AssistantTurn."""

    def __init__(self, script: list[AssistantTurn] | Callable[[list[dict], list[dict]], AssistantTurn]):
        self._script = script
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def complete(self, messages, tools):
        self.calls.append((json.loads(json.dumps(messages, default=str)), tools))
        if callable(self._script):
            return self._script(messages, tools)
        return self._script.pop(0)

    async def aclose(self) -> None:
        return None
