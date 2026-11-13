"""One recipe for assembling services, shared by REST and MCP (Chapter 2/7).

Both transports call `build_order_service(session, container)`. Neither
duplicates wiring, and tests can build the same graph with fake connectors.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.repositories.repos import IdempotencyRepository, OrderRepository, ProductRepository
from app.services.order_service import InventoryService, OrderService


def build_order_service(session: AsyncSession, c: Container) -> OrderService:
    return OrderService(session, OrderRepository(session), ProductRepository(session),
                        IdempotencyRepository(session), c.payment, c.crm)


def build_inventory_service(session: AsyncSession) -> InventoryService:
    return InventoryService(ProductRepository(session))
