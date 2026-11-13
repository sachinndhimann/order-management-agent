"""Glue: build a per-request agent for a specific user (Chapter 7).

TOKEN PROPAGATION: we forward the *caller's own* JWT to our MCP server. The
server therefore sees the real user, and a customer's agent genuinely cannot
call tools the customer isn't allowed to use. (Never give an LLM agent a
god-mode service token and hope the prompt keeps it in line.)
"""
from __future__ import annotations

from app.agent.agent import AgentResult, McpToolHub, OrderAgent
from app.container import Container
from app.connectors.mcp_external import McpServerConnector


class AgentService:
    def __init__(self, container: Container):
        self.c = container

    def _servers(self, user_token: str) -> list[McpServerConnector]:
        s = self.c.settings
        # mcp_overrides lets tests swap URLs for in-process FastMCP instances.
        orders_t = self.c.mcp_overrides.get("orders", s.mcp_server_url)
        logistics_t = self.c.mcp_overrides.get("logistics", s.external_mcp_url)
        return [
            McpServerConnector("orders", orders_t, auth=user_token),
            McpServerConnector("logistics", logistics_t, auth=s.external_mcp_api_key.get_secret_value()),
        ]

    async def chat(self, message: str, user_token: str, history: list[dict] | None = None) -> AgentResult:
        async with McpToolHub(self._servers(user_token)) as hub:
            agent = OrderAgent(self.c.llm, hub, max_steps=self.c.settings.agent_max_steps)
            return await agent.run(message, history)
