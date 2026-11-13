"""Chapters 4 & 5 - MCP and FastMCP: discovery, schemas, structured output, errors, context, transports, auth, security.

Two ways to test an MCP server (both used here):
  1. IN-MEMORY: Client(server_object) - no sockets, fast, ideal for tool logic.
  2. REAL HTTP: serve the ASGI app with uvicorn on a free port - needed for auth/transport behaviour.
"""
import json

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from app.core.security import Role
from app.mcp_server.server import create_server
from tests.conftest import make_token

ADMIN_TOOLS = {"get_order", "list_orders", "check_inventory", "create_order", "update_order_status", "sales_summary"}


@pytest.fixture
async def mcp(container):
    return create_server(container.settings, auth_enabled=False)  # in-memory => no HTTP auth layer


# ------------------------------------------------------------------ discovery & schemas (Ch 4)
async def test_tool_discovery_lists_tools_with_json_schema(mcp):
    async with Client(mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert set(tools) == ADMIN_TOOLS
    schema = tools["create_order"].inputSchema
    assert set(schema["required"]) >= {"customer_email", "items"}
    assert "svc" not in schema["properties"] and "who" not in schema["properties"]  # DI params hidden from the LLM
    assert schema["properties"]["idempotency_key"]["description"]
    assert tools["get_order"].description.startswith("Retrieve one order")
    assert tools["get_order"].annotations.readOnlyHint is True
    assert tools["update_order_status"].annotations.destructiveHint is True
    assert tools["check_inventory"].outputSchema  # typed return => output schema published


async def test_resources_and_prompts_are_discoverable(mcp):
    async with Client(mcp) as c:
        assert "inventory://catalog" in {str(r.uri) for r in await c.list_resources()}
        assert "orders://{order_id}" in {t.uriTemplate for t in await c.list_resource_templates()}
        assert "triage_order" in {p.name for p in await c.list_prompts()}
        catalog = json.loads((await c.read_resource("inventory://catalog"))[0].text)
        assert {p["sku"] for p in catalog} >= {"KB-100", "MN-300"}
        prompt = await c.get_prompt("triage_order", {"order_id": "abc"})
        assert "abc" in prompt.messages[0].content.text


# ------------------------------------------------------------------ invocation lifecycle (Ch 4/5)
async def test_call_tool_returns_structured_and_text_content(mcp):
    async with Client(mcp) as c:
        res = await c.call_tool("check_inventory", {"sku": "kb-100"})
    assert res.structured_content == {"sku": "KB-100", "name": "Mechanical Keyboard", "available": 25, "in_stock": True}
    assert res.data.available == 25          # FastMCP re-hydrates a typed object client-side
    assert "KB-100" in res.content[0].text   # plain-text fallback for clients without structured output


async def test_full_order_lifecycle_through_tools(mcp, container):
    async with Client(mcp) as c:
        created = await c.call_tool("create_order", {"customer_email": "ada@example.com",
                                    "items": [{"sku": "MS-200", "quantity": 2}], "idempotency_key": "mcp-key-0001"})
        oid = created.structured_content["id"]
        assert created.structured_content["status"] == "paid"
        again = await c.call_tool("create_order", {"customer_email": "ada@example.com",
                                  "items": [{"sku": "MS-200", "quantity": 2}], "idempotency_key": "mcp-key-0001"})
        assert again.structured_content["id"] == oid  # retry-safe for LLMs that call twice
        got = await c.call_tool("get_order", {"order_id": oid})
        assert got.structured_content["total_cents"] == 2 * 2999
        lst = await c.call_tool("list_orders", {"status": "paid", "limit": 5})
        assert lst.structured_content["total"] == 1
        shipped = await c.call_tool("update_order_status", {"order_id": oid, "status": "shipped"})
        assert shipped.structured_content["status"] == "shipped"
        res = await c.read_resource(f"orders://{oid}")
        assert json.loads(res[0].text)["status"] == "shipped"
    assert len(container.payment.charges) == 1


# ------------------------------------------------------------------ errors (Ch 5)
async def test_domain_errors_become_tool_errors_with_codes(mcp):
    async with Client(mcp) as c:
        with pytest.raises(ToolError, match=r"\[not_found\]"):
            await c.call_tool("check_inventory", {"sku": "NOPE-1"})
        with pytest.raises(ToolError, match=r"\[insufficient_stock\]"):
            await c.call_tool("create_order", {"customer_email": "a@b.co", "items": [{"sku": "HD-400", "quantity": 50}]})
        res = await c.call_tool("get_order", {"order_id": "0" * 36}, raise_on_error=False)
        assert res.is_error and "not_found" in res.content[0].text      # error is DATA the LLM can read


async def test_input_validation_happens_before_the_tool_runs(mcp, container):
    async with Client(mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool("create_order", {"customer_email": "a@b.co", "items": [{"sku": "KB-100", "quantity": 0}]})
        with pytest.raises(ToolError):
            await c.call_tool("get_order", {"order_id": "short"})
    assert container.payment.charges == []


async def test_unexpected_exception_is_masked(mcp, container):
    async def boom(**k):
        raise RuntimeError("db password=hunter2")
    container.payment.charge = boom
    async with Client(mcp) as c:
        with pytest.raises(ToolError) as e:
            await c.call_tool("create_order", {"customer_email": "a@b.co", "items": [{"sku": "KB-100", "quantity": 1}]})
    assert "hunter2" not in str(e.value)


# ------------------------------------------------------------------ context & logging (Ch 5)
async def test_context_logging_reaches_client(mcp):
    logs: list[str] = []

    async def on_log(msg):
        logs.append(str(msg.data))

    async with Client(mcp, log_handler=on_log) as c:
        await c.call_tool("create_order", {"customer_email": "a@b.co", "items": [{"sku": "KB-100", "quantity": 1}]})
    assert any("creating order" in m for m in logs) and any("order created" in m for m in logs)


# ------------------------------------------------------------------ testing a bare FastMCP tool (minimal example, Ch 5)
async def test_minimal_fastmcp_tool_example():
    demo = FastMCP("demo")

    @demo.tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    async with Client(demo) as c:
        assert (await c.call_tool("add", {"a": 2, "b": 3})).data == 5
        with pytest.raises(ToolError):
            await c.call_tool("add", {"a": "two", "b": 3})  # wrong type rejected by the generated schema


# ------------------------------------------------------------------ Streamable HTTP + auth + authorization (Ch 4/5)
async def test_http_requires_a_valid_token(http_url, settings):
    with pytest.raises(Exception):
        async with Client(http_url) as c:  # no token
            await c.list_tools()
    with pytest.raises(Exception):
        async with Client(http_url, auth="not-a-jwt") as c:
            await c.list_tools()
    async with Client(http_url, auth=make_token(settings, Role.STAFF)) as c:
        assert {t.name for t in await c.list_tools()} == ADMIN_TOOLS - {"sales_summary"}  # staff lacks reports:read


async def test_scopes_hide_and_block_tools_by_role(http_url, settings):
    async with Client(http_url, auth=make_token(settings, Role.CUSTOMER)) as c:
        names = {t.name for t in await c.list_tools()}
        assert "check_inventory" in names and "get_order" in names
        assert "create_order" not in names and "sales_summary" not in names     # least privilege: not even advertised
        with pytest.raises(ToolError):
            await c.call_tool("create_order", {"customer_email": "ada@example.com", "items": [{"sku": "KB-100", "quantity": 1}]})
    async with Client(http_url, auth=make_token(settings, Role.ADMIN)) as c:
        assert {t.name for t in await c.list_tools()} == ADMIN_TOOLS


async def test_http_identity_flows_into_data_authorization(http_url, settings, container):
    """The token's identity, not the model's request, decides which orders are visible."""
    async with Client(http_url, auth=make_token(settings, Role.STAFF)) as staff:
        oid = (await staff.call_tool("create_order", {"customer_email": "ada@example.com",
               "items": [{"sku": "KB-100", "quantity": 1}]})).structured_content["id"]
    async with Client(http_url, auth=make_token(settings, Role.CUSTOMER, email="eve@example.com", user_id=9)) as eve:
        res = await eve.call_tool("get_order", {"order_id": oid}, raise_on_error=False)
        assert res.is_error and "not_found" in res.content[0].text
    async with Client(http_url, auth=make_token(settings, Role.CUSTOMER)) as ada:
        assert (await ada.call_tool("get_order", {"order_id": oid})).structured_content["id"] == oid


async def test_http_session_is_established(http_url, settings):
    async with Client(http_url, auth=make_token(settings, Role.ADMIN)) as c:
        assert c.is_connected()
        assert c.server_info.name == "order-management"
        assert c.server_capabilities.tools is not None and c.server_capabilities.resources is not None
        assert "Order management" in (c.instructions or "")


# ------------------------------------------------------------------ MCP security considerations
async def test_resource_template_rejects_path_traversal(mcp):
    async with Client(mcp) as c:
        with pytest.raises(Exception):
            await c.read_resource("orders://../../etc/passwd")


async def test_reporting_connector_only_allows_whitelisted_queries(container):
    with pytest.raises(KeyError):
        await container.reporting.run("anything; DROP TABLE orders")
    assert isinstance(await container.reporting.low_stock(5), list)
