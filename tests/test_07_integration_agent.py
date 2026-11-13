"""Chapter 7 - Integration: LLM tool calling over MCP -> services -> DB, with an external MCP server and connectors.

The LLM is scripted (deterministic, free), EVERYTHING else is real: agent loop, MCP protocol (in-memory or real
HTTP), FastMCP tools, services, repositories, database, connectors (fakes only for the outside vendors).
"""
import json

import httpx
import pytest
import respx
from sqlalchemy import select

from app.agent.agent import McpToolHub, OrderAgent
from app.agent.llm import AssistantTurn, OpenAICompatibleLLM, ScriptedLLM, ToolCall
from app.connectors.mcp_external import McpServerConnector
from app.core.config import Settings
from app.core.security import Role
from app.db.models import Order
from app.db.session import session_scope
from app.mcp_server.server import create_server
from external_mcp.logistics_server import create_logistics_server
from tests.conftest import make_token


def hub_for(container, **kw):
    return McpToolHub([
        McpServerConnector("orders", create_server(container.settings, auth_enabled=False)),
        McpServerConnector("logistics", create_logistics_server("k")),
    ])


def call(name, **args):
    return AssistantTurn(tool_calls=[ToolCall(f"call_{name}", name, args)])


async def test_agent_discovers_tools_from_both_servers(container):
    llm = ScriptedLLM([AssistantTurn(content="ok")])
    async with hub_for(container) as hub:
        await OrderAgent(llm, hub).run("hi")
    _, tools = llm.calls[0]
    names = {t["function"]["name"] for t in tools}
    assert {"orders__get_order", "orders__check_inventory", "orders__create_order", "orders__update_order_status",
            "logistics__track_shipment"} <= names
    create = next(t for t in tools if t["function"]["name"] == "orders__create_order")["function"]
    assert create["parameters"]["required"] and "items" in create["parameters"]["properties"]  # JSON Schema reaches the LLM


async def test_agent_checks_inventory_creates_order_and_ships_it(container):
    seen_results = []

    def script(messages, tools):
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        seen_results[:] = [m["content"] for m in tool_msgs]
        n = len(tool_msgs)
        if n == 0:
            return call("orders__check_inventory", sku="MN-300")
        if n == 1:
            assert '"available":8' in tool_msgs[0]["content"].replace(" ", "")   # the model can SEE tool output
            return call("orders__create_order", customer_email="ada@example.com",
                        items=[{"sku": "MN-300", "quantity": 1}], idempotency_key="agent-run-0001")
        if n == 2:
            oid = json.loads(tool_msgs[1]["content"])["id"]
            return call("orders__update_order_status", order_id=oid, status="shipped")
        return AssistantTurn(content="Order created and shipped.")

    async with hub_for(container) as hub:
        result = await OrderAgent(ScriptedLLM(script), hub).run("Order one MN-300 for ada and ship it")
    assert result.answer == "Order created and shipped." and result.steps == 4
    assert [t.tool for t in result.trace] == ["orders__check_inventory", "orders__create_order", "orders__update_order_status"]
    async with session_scope(container.sessionmaker) as s:
        order = (await s.execute(select(Order))).scalar_one()
    assert order.status == "shipped" and order.total_cents == 24999 and order.payment_ref == "ch_1"
    assert container.payment.charges and container.crm.upserts == ["ada@example.com"]


async def test_tool_errors_are_returned_to_the_model_not_raised(container):
    def script(messages, tools):
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if not tool_msgs:
            return call("orders__create_order", customer_email="ada@example.com", items=[{"sku": "HD-400", "quantity": 99}])
        assert "insufficient_stock" in tool_msgs[0]["content"]     # self-correction material
        return AssistantTurn(content="Only 2 HD-400 left; want 2?")

    async with hub_for(container) as hub:
        r = await OrderAgent(ScriptedLLM(script), hub).run("buy 99 hubs")
    assert r.trace[0].is_error and "left" in r.answer


async def test_external_mcp_tool_is_invoked_via_the_same_hub(container):
    llm = ScriptedLLM([call("logistics__track_shipment", tracking_number="AC12345678"), AssistantTurn(content="done")])
    async with hub_for(container) as hub:
        r = await OrderAgent(llm, hub).run("where is AC12345678?")
    assert r.trace[0].tool == "logistics__track_shipment" and "ACME Freight" in r.trace[0].result


async def test_unknown_tool_and_bad_json_arguments_are_recoverable(container):
    llm = ScriptedLLM([
        AssistantTurn(tool_calls=[ToolCall("1", "orders__drop_database", {})]),
        AssistantTurn(tool_calls=[ToolCall("2", "orders__get_order", {"__invalid_json__": "{oops"})]),
        AssistantTurn(content="sorry")])
    async with hub_for(container) as hub:
        r = await OrderAgent(llm, hub).run("x")
    assert r.trace[0].is_error and "Unknown tool" in r.trace[0].result
    assert r.trace[1].is_error and "valid JSON" in r.trace[1].result and r.answer == "sorry"


async def test_step_limit_stops_runaway_loops(container):
    llm = ScriptedLLM(lambda m, t: call("orders__check_inventory", sku="KB-100"))
    async with hub_for(container) as hub:
        r = await OrderAgent(llm, hub, max_steps=3).run("loop forever")
    assert r.steps == 3 and len(r.trace) == 3 and "step limit" in r.answer


async def test_dead_external_server_degrades_instead_of_crashing(container):
    dead = McpServerConnector("logistics", "http://127.0.0.1:1/mcp", timeout=1)
    hub = McpToolHub([McpServerConnector("orders", create_server(container.settings, auth_enabled=False)), dead])
    llm = ScriptedLLM([call("orders__check_inventory", sku="KB-100"), AssistantTurn(content="25 in stock")])
    async with hub:
        r = await OrderAgent(llm, hub).run("stock?")
    assert r.unavailable_servers == ["logistics"] and r.answer == "25 in stock"
    names = {t["function"]["name"] for t in llm.calls[0][1]}
    assert not any(n.startswith("logistics__") for n in names)


# ------------------------------------------------------------------ REST -> agent -> real HTTP MCP with the user's own token
async def test_rest_agent_endpoint_propagates_user_token_and_enforces_rbac(client, container, http_url, auth, settings):
    """A CUSTOMER asks the agent to create an order. The model tries; the MCP server (not the prompt) refuses."""
    container.mcp_overrides = {"orders": http_url, "logistics": create_logistics_server("k")}
    container.llm = ScriptedLLM(lambda msgs, tools: (
        call("orders__create_order", customer_email="ada@example.com", items=[{"sku": "KB-100", "quantity": 1}])
        if not any(m["role"] == "tool" for m in msgs) else AssistantTurn(content="I can't do that for you.")))

    cust = await client.post("/api/v1/agent/chat", json={"message": "buy a keyboard"}, headers=auth(Role.CUSTOMER))
    assert cust.status_code == 200
    body = cust.json()
    assert "create_order" not in " ".join(t["function"]["name"] for t in container.llm.calls[0][1])   # not even offered
    assert body["tool_calls"][0]["is_error"] is True and container.payment.charges == []

    # Same request as STAFF: tool is offered and succeeds
    container.llm = ScriptedLLM(lambda msgs, tools: (
        call("orders__create_order", customer_email="ada@example.com", items=[{"sku": "KB-100", "quantity": 1}])
        if not any(m["role"] == "tool" for m in msgs) else AssistantTurn(content="Done.")))
    staff = await client.post("/api/v1/agent/chat", json={"message": "buy a keyboard"}, headers=auth(Role.STAFF))
    assert staff.json()["tool_calls"][0]["is_error"] is False and len(container.payment.charges) == 1


async def test_agent_endpoint_requires_auth(client):
    assert (await client.post("/api/v1/agent/chat", json={"message": "hi"})).status_code == 401
    assert (await client.post("/api/v1/agent/chat", json={"message": ""}, headers={"Authorization": "Bearer x"})).status_code in (401, 422)


# ------------------------------------------------------------------ the real LLM client (HTTP mocked)
@respx.mock
async def test_openai_compatible_client_parses_tool_calls_and_bad_json():
    llm = OpenAICompatibleLLM(Settings(llm_base_url="http://llm.test/v1", _env_file=None))
    route = respx.post("http://llm.test/v1/chat/completions").mock(return_value=httpx.Response(200, json={"choices": [{"message": {
        "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "orders__get_order", "arguments": '{"order_id": "x"}'}},
            {"id": "b", "type": "function", "function": {"name": "orders__get_order", "arguments": "{broken"}}]}}]}))
    turn = await llm.complete([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "t"}}])
    assert turn.tool_calls[0].arguments == {"order_id": "x"} and "__invalid_json__" in turn.tool_calls[1].arguments
    sent = json.loads(route.calls.last.request.content)
    assert sent["tool_choice"] == "auto" and sent["temperature"] == 0 and sent["tools"]
    assert route.calls.last.request.headers["Authorization"].startswith("Bearer ")
    await llm.aclose()


def test_token_helper_roundtrip(settings):
    assert make_token(settings, Role.ADMIN).count(".") == 2
    pytest.importorskip("jwt")
