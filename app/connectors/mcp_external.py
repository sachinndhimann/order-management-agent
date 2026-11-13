"""External MCP server connector (Chapter 4/6).

An MCP server is "just another dependency" for resilience purposes: it can be
slow, down, or reject our credentials. This wrapper gives it the same timeout /
retry / breaker treatment as the HTTP connectors, and normalises tool metadata
into plain dataclasses so the agent code never touches SDK types.

SESSION LIFECYCLE (Chapter 4): `async with connector:` performs the MCP
handshake (initialize -> initialized), keeps the session open for many calls,
then closes it. Streamable HTTP assigns an `Mcp-Session-Id` during initialize.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client

from app.connectors.base import (
    CircuitBreaker, CircuitOpenError, ConnectorTimeout, ConnectorUnavailable, RetryPolicy,
)


@dataclass(frozen=True)
class McpToolSpec:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class McpCallResult:
    text: str
    data: Any = None
    is_error: bool = False
    raw: dict = field(default_factory=dict)


class McpServerConnector:
    def __init__(self, name: str, transport: Any, *, auth: str | None = None,
                 timeout: float = 15.0, retry: RetryPolicy | None = None,
                 breaker: CircuitBreaker | None = None, sleep=asyncio.sleep):
        """`transport` = URL string ("http://host:8002/mcp"), a script path
        (stdio) or an in-process FastMCP instance (tests). `auth` = bearer token."""
        self.name = name
        # A bearer token only makes sense on HTTP transports (stdio/in-memory have no headers).
        is_http = isinstance(transport, str) and transport.startswith("http")
        self._client = Client(transport, auth=auth if is_http else None, timeout=timeout)
        self._retry = retry or RetryPolicy(max_attempts=2)
        self._breaker = breaker or CircuitBreaker()
        self._sleep = sleep

    async def __aenter__(self) -> McpServerConnector:
        await self._guarded(self._client.__aenter__)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.__aexit__(*exc)

    async def _guarded(self, fn, *args, **kwargs):
        """breaker + retry around any MCP round-trip."""
        last: Exception | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            if not self._breaker.allow():
                raise CircuitOpenError(f"mcp:{self.name} circuit open")
            try:
                result = await fn(*args, **kwargs)
                self._breaker.record_success()
                return result
            except TimeoutError as exc:
                last = ConnectorTimeout(f"mcp:{self.name} timed out")
                last.__cause__ = exc
            except (ConnectionError, OSError) as exc:
                last = ConnectorUnavailable(f"mcp:{self.name} unreachable: {exc}")
                last.__cause__ = exc
            self._breaker.record_failure()
            if attempt < self._retry.max_attempts:
                await self._sleep(self._retry.delay(attempt))
        assert last is not None
        raise last

    async def list_tools(self) -> list[McpToolSpec]:
        # Discovery: tools/list. The server is the source of truth; we never hard-code tools.
        tools = await self._guarded(self._client.list_tools)
        return [McpToolSpec(self.name, t.name, t.description or "", t.inputSchema) for t in tools]

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> McpCallResult:
        # raise_on_error=False: tool failures are DATA for the LLM ("order not found"),
        # not exceptions that crash the agent loop. Transport failures still raise.
        res = await self._guarded(self._client.call_tool, tool, arguments, raise_on_error=False)
        text = "\n".join(getattr(b, "text", "") for b in res.content if getattr(b, "text", None))
        return McpCallResult(text=text, data=res.structured_content, is_error=res.is_error)
