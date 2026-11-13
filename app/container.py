"""Composition root: the ONE place where concrete objects are wired together.

Inversion of Control (Chapter 2): OrderService does not create its own
PaymentGateway or session; it *receives* them. Whoever builds the service —
FastAPI's Depends, an MCP tool's Depends, or a unit test — chooses the
implementations. This module holds the long-lived, process-wide pieces
(engine, HTTP clients); per-request pieces are built in the dependency
providers (app/api/deps.py and app/mcp_server/server.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent.llm import LLMClient, OpenAICompatibleLLM
from app.connectors.crm import CRMConnector, CRMPort
from app.connectors.payment import PaymentConnector, PaymentPort
from app.connectors.reporting import ReportingConnector
from app.connectors.shipping import ShippingConnector, ShippingPort
from app.core.config import Settings, get_settings
from app.db.session import make_engine, make_sessionmaker


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    payment: PaymentPort
    crm: CRMPort
    shipping: ShippingPort
    reporting: ReportingConnector
    llm: LLMClient
    mcp_overrides: dict = field(default_factory=dict)  # name -> transport (tests inject FastMCP objects)
    _closers: list = field(default_factory=list)

    async def aclose(self) -> None:
        for closer in self._closers:
            await closer()
        await self.engine.dispose()


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    engine = make_engine(settings)
    payment = PaymentConnector.from_settings(settings)
    crm = CRMConnector.from_settings(settings)
    shipping = ShippingConnector.from_settings(settings)
    reporting = ReportingConnector.from_settings(settings, fallback_engine=engine)
    llm = OpenAICompatibleLLM(settings)
    return Container(
        settings=settings,
        engine=engine,
        sessionmaker=make_sessionmaker(engine),
        payment=payment,
        crm=crm,
        shipping=shipping,
        reporting=reporting,
        llm=llm,
        _closers=[payment.aclose, crm.aclose, shipping.aclose, reporting.aclose, llm.aclose],
    )


# Process-wide holder. Set in the app lifespan; tests call set_container().
_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = build_container()
    return _container


def set_container(c: Container | None) -> None:
    global _container
    _container = c


async def close_container() -> None:
    global _container
    if _container is not None:
        await _container.aclose()
        _container = None
