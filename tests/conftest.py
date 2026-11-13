"""Shared fixtures. Philosophy: real DB (in-memory SQLite) + FAKE external systems.

The unit under test is our code, so the outside world (payment, CRM, shipping,
LLM) is replaced through the very same ports production uses (Chapter 2/6).
"""
from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: F401

from app.agent.llm import ScriptedLLM
from app.connectors.crm import CustomerProfile
from app.connectors.payment import PaymentResult, PaymentStatus
from app.connectors.reporting import ReportingConnector
from app.connectors.shipping import ShippingQuote
from app.container import Container, set_container
from app.core.config import Settings
from app.core.errors import PaymentFailedError, UpstreamUnavailableError
from app.core.security import Principal, Role, create_access_token
from app.db.models import Base
from app.db.session import make_engine, make_sessionmaker
from app.main import create_app
from app.mcp_server.server import create_server
from app.seed import seed


class FakePayment:
    def __init__(self):
        self.charges: list[dict] = []
        self.refunds: list[dict] = []
        self.decline = False
        self.down = False

    async def charge(self, *, order_id, amount_cents, currency, customer_email):
        if self.down:
            raise UpstreamUnavailableError("payment down")
        if self.decline:
            raise PaymentFailedError("Payment was declined")
        self.charges.append({"order_id": order_id, "amount": amount_cents})
        return PaymentResult(f"ch_{len(self.charges)}", PaymentStatus.SUCCEEDED, amount_cents)

    async def refund(self, *, payment_ref, amount_cents):
        self.refunds.append({"ref": payment_ref, "amount": amount_cents})
        return "re_1"

    async def aclose(self): ...


class FakeCRM:
    def __init__(self):
        self.down = False
        self.upserts: list[str] = []

    async def upsert_customer(self, *, email, full_name=None):
        if self.down:
            raise UpstreamUnavailableError("crm down")
        self.upserts.append(email)
        return CustomerProfile("c-1", email, full_name or email, "gold")

    async def get_customer(self, email):
        if self.down:
            raise UpstreamUnavailableError("crm down")
        return CustomerProfile("c-1", email, email, "gold")

    async def aclose(self): ...


class FakeShipping:
    def __init__(self):
        self.down = False

    async def quote(self, *, postal_code, weight_kg):
        if self.down:
            raise UpstreamUnavailableError("shipping down")
        return ShippingQuote("TestCarrier", 999, 3)

    async def aclose(self): ...


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///:memory:", environment="test", _env_file=None)


async def build_test_container(settings: Settings) -> Container:
    engine = make_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = make_sessionmaker(engine)
    await seed(maker)
    c = Container(settings=settings, engine=engine, sessionmaker=maker, payment=FakePayment(), crm=FakeCRM(),
                  shipping=FakeShipping(), reporting=ReportingConnector(engine), llm=ScriptedLLM([]))
    set_container(c)  # MCP tools resolve their services through the global container
    return c


@pytest.fixture
async def container(settings):
    c = await build_test_container(settings)
    yield c
    set_container(None)
    await c.engine.dispose()


@pytest.fixture
async def file_container(tmp_path):
    """File-backed SQLite: every session gets its OWN connection, so concurrency tests are real.
    (In-memory + StaticPool shares ONE connection, i.e. one transaction, between sessions.)"""
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}", environment="test", _env_file=None)
    c = await build_test_container(s)
    yield c
    set_container(None)
    await c.engine.dispose()


@pytest.fixture
async def client(container):
    app = create_app(container)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.app = app  # convenient handle for dependency_overrides
        yield ac


def make_token(settings: Settings, role: Role = Role.STAFF, email: str | None = None, user_id: int | None = None) -> str:
    ids = {Role.ADMIN: 1, Role.STAFF: 2, Role.CUSTOMER: 3}
    emails = {Role.ADMIN: "admin@example.com", Role.STAFF: "staff@example.com", Role.CUSTOMER: "ada@example.com"}
    return create_access_token(Principal(user_id or ids[role], email or emails[role], role), settings)


@pytest.fixture
def auth(settings):
    def _h(role: Role = Role.STAFF, **kw) -> dict:
        return {"Authorization": f"Bearer {make_token(settings, role, **kw)}"}
    return _h


ORDER_BODY = {"customer_email": "ada@example.com", "items": [{"sku": "KB-100", "quantity": 2}]}


@pytest.fixture
async def http_url(container):
    """Serve the real server over Streamable HTTP with JWT auth on an ephemeral port."""
    server = create_server(container.settings, auth_enabled=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = uvicorn.Config(server.http_app(path="/mcp"), host="127.0.0.1", port=port, log_level="error")
    uv = uvicorn.Server(cfg)
    task = asyncio.create_task(uv.serve())
    for _ in range(100):
        if uv.started:
            break
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    uv.should_exit = True
    await task


