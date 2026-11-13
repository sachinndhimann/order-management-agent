"""FastMCP server exposing the Order Management domain to LLMs (Chapters 4 & 5).

MCP ARCHITECTURE (who is who)
    Host    = the app that embeds the LLM        -> our FastAPI `/agent/chat`
    Client  = connector inside the host, 1 per server -> app/connectors/mcp_external.py
    Server  = process exposing capabilities      -> THIS file

THREE capability types, distinguished by WHO controls them:
    Tools     model-controlled   actions / queries the LLM decides to call
    Resources app-controlled     read-only data addressed by URI (context to attach)
    Prompts   user-controlled    reusable prompt templates (slash-command style)

LIFECYCLE (JSON-RPC 2.0 messages)
    client -> initialize {protocolVersion, capabilities}
    server -> result     {capabilities: tools/resources/prompts, serverInfo}
    client -> notifications/initialized
    client -> tools/list                      (discovery; JSON Schema per tool)
    client -> tools/call {name, arguments}    (invocation)
    server -> result {content: [...], structuredContent: {...}, isError}
Over HTTP the server issues an `Mcp-Session-Id` header at initialize.

TRANSPORTS
    stdio            server is a child process; JSON-RPC over stdin/stdout.
                     No network, no auth needed (the OS user is the trust boundary).
    streamable HTTP  one endpoint (/mcp); POST for messages, optional SSE stream
                     for server->client. Needs real auth (bearer JWT here).

THE KEY DESIGN RULE: tools contain NO business logic. They authenticate, call the
same services the REST API calls, and translate errors. That is why the REST API
and the LLM can never disagree about what is allowed.
"""
from __future__ import annotations

import argparse
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.dependencies import CurrentContext, Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import JWTVerifier, require_scopes
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import ToolAnnotations
from pydantic import EmailStr, Field

from app.container import close_container, get_container
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.observability import MCP_TOOL_CALLS, log_event
from app.core.security import ROLE_SCOPES, Principal, Role
from app.db.models import OrderStatus
from app.db.session import session_scope
from app.schemas.common import Page
from app.schemas.orders import OrderCreate, OrderFilter, OrderItemIn, OrderOut, StockOut
from app.services.factory import build_inventory_service, build_order_service
from app.services.order_service import InventoryService, OrderService

log = logging.getLogger("mcp")


# ===================================================================== dependency injection (Chapter 2)
# fastmcp's Depends mirrors FastAPI's: parameters annotated with Depends() are
# resolved per call and are NOT part of the tool's JSON Schema (the LLM never sees them).

@asynccontextmanager
async def get_order_service() -> AsyncIterator[OrderService]:
    """Yield dependency: one DB session/transaction per TOOL CALL. fastmcp (unlike FastAPI)
    wants an *async context manager*, hence @asynccontextmanager; cleanup = code after yield."""
    c = get_container()
    async with session_scope(c.sessionmaker) as session:
        yield build_order_service(session, c)


@asynccontextmanager
async def get_inventory_service() -> AsyncIterator[InventoryService]:
    async with session_scope(get_container().sessionmaker) as session:
        yield build_inventory_service(session)


def get_principal() -> Principal:
    """Who is calling? Built from the verified JWT claims (HTTP transport).
    With stdio there is no token: the caller is the local operator -> admin."""
    token = get_access_token()
    if token is None:
        return Principal(0, "stdio@local", Role.ADMIN)
    claims = token.claims
    return Principal(int(claims["sub"]), claims["email"], Role(claims["role"]))


# NOTE: fastmcp resolves `param: T = Depends(fn)` (default-value style). The
# FastAPI-style `Annotated[T, Depends(fn)]` alias is not used in this codebase for MCP tools.


# ===================================================================== middleware: errors + metrics
class DomainErrorMiddleware(Middleware):
    """Translate domain errors into MCP ToolErrors in ONE place.

    ToolError text is returned to the LLM (isError=true) so it can self-correct
    ("insufficient_stock for KB-100 -> offer a smaller quantity"). Any other
    exception is *masked* by mask_error_details=True so internals never leak."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool = context.message.name
        try:
            result = await call_next(context)
            MCP_TOOL_CALLS.labels(tool, "ok").inc()
            return result
        except AppError as exc:
            MCP_TOOL_CALLS.labels(tool, exc.code).inc()
            raise ToolError(f"[{exc.code}] {exc.message}") from exc
        except ToolError as exc:
            # With mask_error_details=True, FastMCP has ALREADY converted our AppError into a generic
            # "Error calling tool" ToolError (original kept as __cause__). Recover it so the LLM
            # gets the actionable "[code] message" instead of a useless generic string.
            cause = exc.__cause__
            if isinstance(cause, AppError):
                MCP_TOOL_CALLS.labels(tool, cause.code).inc()
                raise ToolError(f"[{cause.code}] {cause.message}") from cause
            MCP_TOOL_CALLS.labels(tool, "tool_error").inc()
            raise
        except Exception:
            MCP_TOOL_CALLS.labels(tool, "internal_error").inc()
            log.exception("mcp_tool_crashed", extra={"extra_fields": {"tool": tool}})
            raise


# ===================================================================== server factory
def create_server(settings: Settings | None = None, *, auth_enabled: bool = True,
                  close_pools_on_shutdown: bool = False) -> FastMCP:
    settings = settings or get_settings()

    verifier = None
    if auth_enabled:
        # Validates signature/expiry/issuer/audience of the bearer JWT on every HTTP request.
        # Production: JWTVerifier(jwks_uri=...) with an asymmetric IdP; no shared secret.
        verifier = JWTVerifier(public_key=settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm,
                               issuer="order-agent", audience="order-agent")

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        yield
        # Lifespan runs once per SERVER PROCESS over HTTP, but once per client connection for the
        # in-memory test transport. Disposing shared pools there would break the next connection,
        # so closing is opt-in and only enabled by the real entrypoint (main()).
        if close_pools_on_shutdown:
            await close_container()

    mcp = FastMCP(
        "order-management",
        instructions="Order management tools: look up orders, check inventory, create orders, change order status. "
                     "Amounts are integer cents. Order ids are UUIDs.",
        auth=verifier,
        middleware=[DomainErrorMiddleware()],
        mask_error_details=True,
        lifespan=lifespan,
    )

    def scoped(*scopes: str):
        """Per-tool authorization: token must carry these scopes. In stdio mode (no auth) skip."""
        return require_scopes(*scopes) if auth_enabled else None

    read_only = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

    # ------------------------------------------------------------------ TOOLS
    # The function NAME, DOCSTRING and TYPE HINTS become the contract the LLM reads:
    # name -> tool name, docstring -> description, hints -> JSON Schema `inputSchema`,
    # return annotation -> `outputSchema` + structuredContent.
    @mcp.tool(annotations=read_only, auth=scoped("orders:read"), tags={"orders"})
    async def get_order(
        order_id: Annotated[str, Field(description="Order UUID, e.g. 3f2b...-...", min_length=36, max_length=36)],
        svc: OrderService = Depends(get_order_service), who: Principal = Depends(get_principal),
    ) -> OrderOut:
        """Retrieve one order (items, status, total in cents, payment reference) by its id."""
        return OrderOut.model_validate(await svc.get_order(order_id, who))

    @mcp.tool(annotations=read_only, auth=scoped("orders:read"), tags={"orders"})
    async def list_orders(
        svc: OrderService = Depends(get_order_service), who: Principal = Depends(get_principal),
        status: Annotated[OrderStatus | None, Field(description="Filter by status")] = None,
        customer_email: Annotated[str | None, Field(description="Filter by customer email (staff only)")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        offset: Annotated[int, Field(ge=0)] = 0,
        sort: Annotated[str, Field(pattern=r"^-?(created_at|total_cents|status)$")] = "-created_at",
    ) -> Page[OrderOut]:
        """List orders, newest first by default. Supports status/email filters and pagination."""
        return await svc.list_orders(OrderFilter(status=status, customer_email=customer_email, sort=sort),
                                     limit=limit, offset=offset, actor=who)

    @mcp.tool(annotations=read_only, auth=scoped("inventory:read"), tags={"inventory"})
    async def check_inventory(
        sku: Annotated[str, Field(description="Product SKU such as KB-100", min_length=2, max_length=64)],
        svc: InventoryService = Depends(get_inventory_service),
    ) -> StockOut:
        """Check how many units of a product are in stock."""
        return await svc.check_stock(sku)

    @mcp.tool(auth=scoped("orders:write"), tags={"orders", "write"},
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    async def create_order(
        customer_email: EmailStr, items: Annotated[list[OrderItemIn], Field(min_length=1)],
        svc: OrderService = Depends(get_order_service), who: Principal = Depends(get_principal), ctx: Context = CurrentContext(),
        idempotency_key: Annotated[str | None, Field(
            description="Unique string for this logical request. Re-using it returns the original order "
                        "instead of creating a duplicate - always pass one.", min_length=8)] = None,
    ) -> OrderOut:
        """Create an order: reserves stock, charges the customer's payment method, returns the paid order.
        Fails with insufficient_stock or payment_failed without side effects."""
        await ctx.info(f"creating order for {customer_email} with {len(items)} line(s)")  # MCP logging notification -> client
        order, created = await svc.create_order(OrderCreate(customer_email=customer_email, items=items), who,
                                                idempotency_key)
        await ctx.info("order created" if created else "idempotent replay: returned existing order")
        return OrderOut.model_validate(order)

    @mcp.tool(auth=scoped("orders:write"), tags={"orders", "write"},
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True))
    async def update_order_status(
        order_id: Annotated[str, Field(min_length=36, max_length=36)],
        status: Annotated[OrderStatus, Field(description="Target status. Allowed moves: pending->paid|cancelled, "
                                                        "paid->shipped|cancelled, shipped->delivered")],
        svc: OrderService = Depends(get_order_service), who: Principal = Depends(get_principal),
    ) -> OrderOut:
        """Move an order to a new status. 'cancelled' refunds the payment and restocks items."""
        return OrderOut.model_validate(await svc.update_status(order_id, status, who))

    @mcp.tool(annotations=read_only, auth=scoped("reports:read"), tags={"reports"})
    async def sales_summary(days: Annotated[int, Field(ge=1, le=365)] = 30) -> list[dict]:
        """Order counts and revenue (cents) per status for the last N days. Admin only."""
        return await get_container().reporting.sales_summary(days)

    # ------------------------------------------------------------------ RESOURCES
    @mcp.resource("inventory://catalog", mime_type="application/json", tags={"inventory"})
    async def catalog() -> list[dict]:
        """Full product catalogue with live stock levels (read-only context)."""
        from app.repositories.repos import ProductRepository
        async with session_scope(get_container().sessionmaker) as s:
            rows, _ = await ProductRepository(s).list(limit=100, offset=0)
            return [{"sku": p.sku, "name": p.name, "price_cents": p.price_cents, "stock": p.stock} for p in rows]

    @mcp.resource("orders://{order_id}", mime_type="application/json")
    async def order_resource(order_id: str) -> dict:
        """One order addressed by URI template - resources are 'GET by URL' for models."""
        async with session_scope(get_container().sessionmaker) as s:
            svc = build_order_service(s, get_container())
            admin = Principal(0, "resource@local", Role.ADMIN)
            return OrderOut.model_validate(await svc.get_order(order_id, admin)).model_dump(mode="json")

    # ------------------------------------------------------------------ PROMPTS
    @mcp.prompt(tags={"orders"})
    def triage_order(order_id: str) -> str:
        """Reusable prompt: investigate a problem order."""
        return (f"Investigate order {order_id}. Fetch it with get_order, check stock of each SKU with "
                f"check_inventory, then summarise status, risks and the next action. Do not change anything "
                f"without asking me first.")

    log_event(log, "mcp_server_built", auth=auth_enabled, scopes=ROLE_SCOPES[Role.ADMIN])
    return mcp


def main() -> None:
    p = argparse.ArgumentParser(description="Order Management MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default="http")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8001)
    args = p.parse_args()
    if args.transport == "stdio":
        # NEVER print to stdout in stdio mode: stdout IS the protocol channel. Logs go to stderr.
        logging.basicConfig(level="INFO")
        create_server(auth_enabled=False).run(transport="stdio", show_banner=False)
    else:
        from app.core.observability import configure_logging
        configure_logging(get_settings().log_level)
        create_server(close_pools_on_shutdown=True).run(transport="http", host=args.host, port=args.port, path="/mcp")


if __name__ == "__main__":
    main()
