"""The AI Order Management Agent (Chapter 7).

EXECUTION FLOW
  1. connect to every MCP server           (initialize handshake, sessions open)
  2. tools/list on each, merge             (DISCOVERY - nothing hard-coded)
  3. convert MCP tool schemas to LLM "functions"
  4. loop (bounded):
        LLM sees messages + tool list
        -> answers in text            => done
        -> asks for tool calls        => we execute via tools/call on the owning
                                         server, append results, ask the LLM again
  5. return final answer + a trace of tool calls

Key idea: the MODEL decides *what* to call; WE decide *whether it is allowed*
(token scopes enforced by the MCP server, a step limit, output truncation).
"""
from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from app.agent.llm import LLMClient
from app.connectors.base import ConnectorError
from app.connectors.mcp_external import McpServerConnector, McpToolSpec
from app.core.observability import MCP_TOOL_CALLS, log_event, span

log = logging.getLogger("agent")

SEP = "__"  # orders__get_order  (server name + tool name; avoids collisions between servers)
MAX_TOOL_OUTPUT = 4000  # protect the context window from giant tool results

SYSTEM_PROMPT = """You are an order-management assistant for an online store.
Use the provided tools to look up orders, check inventory, create orders and update order status.
Rules:
- Never invent order ids, SKUs or prices; call a tool to find out.
- Before cancelling an order or creating one, make sure the user actually asked for it.
- When creating an order always pass a unique idempotency_key (8+ chars), and reuse the same key if you retry the same request.
- If a tool returns an error, explain it plainly and suggest a next step.
- Amounts are in cents; show them to users in dollars.
- Be concise."""


@dataclass
class ToolTrace:
    tool: str
    arguments: dict
    result: str
    is_error: bool = False


@dataclass
class AgentResult:
    answer: str
    trace: list[ToolTrace] = field(default_factory=list)
    steps: int = 0
    unavailable_servers: list[str] = field(default_factory=list)


class McpToolHub:
    """Aggregates several MCP servers behind one tool namespace."""

    def __init__(self, servers: list[McpServerConnector]):
        self._servers = {s.name: s for s in servers}
        self._stack = AsyncExitStack()
        self._connected: dict[str, McpServerConnector] = {}
        self.specs: dict[str, McpToolSpec] = {}
        self.unavailable: list[str] = []

    async def __aenter__(self) -> McpToolHub:
        for name, server in self._servers.items():
            try:
                await self._stack.enter_async_context(server)
                for spec in await server.list_tools():
                    self.specs[f"{name}{SEP}{spec.name}"] = spec
                self._connected[name] = server
            except (ConnectorError, OSError, Exception) as exc:  # one dead server must not kill the agent
                log_event(log, "mcp_server_unavailable", server=name, error=str(exc))
                self.unavailable.append(name)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    def openai_tools(self) -> list[dict]:
        """MCP tool -> OpenAI 'function' definition. Both use JSON Schema for
        parameters, so the translation is nearly 1:1."""
        return [{"type": "function", "function": {
            "name": full, "description": spec.description, "parameters": spec.input_schema}}
            for full, spec in self.specs.items()]

    async def call(self, full_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        spec = self.specs.get(full_name)
        if spec is None:
            return f"Unknown tool '{full_name}'. Available: {', '.join(self.specs)}", True
        try:
            with span("mcp.call_tool", tool=full_name):
                res = await self._connected[spec.server].call_tool(spec.name, arguments)
        except ConnectorError as exc:
            MCP_TOOL_CALLS.labels(full_name, "transport_error").inc()
            return f"Tool server unavailable: {exc}", True
        MCP_TOOL_CALLS.labels(full_name, "error" if res.is_error else "ok").inc()
        return res.text[:MAX_TOOL_OUTPUT], res.is_error


class OrderAgent:
    def __init__(self, llm: LLMClient, hub: McpToolHub, *, max_steps: int = 6, system_prompt: str = SYSTEM_PROMPT):
        self.llm, self.hub, self.max_steps, self.system_prompt = llm, hub, max_steps, system_prompt

    async def run(self, user_message: str, history: list[dict] | None = None) -> AgentResult:
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}, *(history or []),
                                {"role": "user", "content": user_message}]
        tools = self.hub.openai_tools()
        result = AgentResult(answer="", unavailable_servers=list(self.hub.unavailable))
        for step in range(1, self.max_steps + 1):
            result.steps = step
            turn = await self.llm.complete(messages, tools)
            if not turn.tool_calls:
                result.answer = turn.content or ""
                return result
            messages.append({"role": "assistant", "content": turn.content, "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in turn.tool_calls]})
            for call in turn.tool_calls:
                if "__invalid_json__" in call.arguments:
                    text, is_err = "Arguments were not valid JSON; call again with a valid object.", True
                else:
                    text, is_err = await self.hub.call(call.name, call.arguments)
                result.trace.append(ToolTrace(call.name, call.arguments, text, is_err))
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
        # Loop guard: a confused model could call tools forever (cost + side effects).
        result.answer = "I couldn't finish within the step limit. Here is what I did so far; please refine the request."
        return result
