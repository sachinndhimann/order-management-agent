"""Service layer (Chapter 3): business rules, orchestration, transaction boundaries.

Called identically by REST routers and MCP tools - this is how the project
guarantees both interfaces enforce the SAME rules (no logic in routers/tools).
"""
from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.crm import CRMPort
from app.connectors.payment import PaymentPort, PaymentStatus
from app.core.errors import (
    ConflictError, InsufficientStockError, InvalidTransitionError, NotFoundError,
    PaymentFailedError, UpstreamUnavailableError, ValidationFailed,
)
from app.core.observability import ORDERS_CREATED, log_event, span
from app.core.security import Principal, Role
from app.db.models import IdempotencyKey, Order, OrderItem, OrderStatus
from app.repositories.repos import IdempotencyRepository, OrderRepository, ProductRepository
from app.schemas.common import Page
from app.schemas.orders import OrderCreate, OrderFilter, OrderOut, StockOut

log = logging.getLogger("orders")

# The state machine as data. Easy to read, test and extend.
TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.PAID, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


class InventoryService:
    def __init__(self, products: ProductRepository):
        self.products = products

    async def check_stock(self, sku: str) -> StockOut:
        p = await self.products.get_by_sku(sku.strip().upper())
        if p is None:
            raise NotFoundError(f"Unknown SKU '{sku}'", sku=sku)
        return StockOut(sku=p.sku, name=p.name, available=p.stock, in_stock=p.stock > 0)


class OrderService:
    """All collaborators are constructor-injected (Chapter 2)."""

    def __init__(self, session: AsyncSession, orders: OrderRepository, products: ProductRepository,
                 idempotency: IdempotencyRepository, payment: PaymentPort, crm: CRMPort):
        self.session, self.orders, self.products = session, orders, products
        self.idempotency, self.payment, self.crm = idempotency, payment, crm

    # ---------------------------------------------------------------- reads
    async def get_order(self, order_id: str, actor: Principal) -> Order:
        order = await self.orders.get(order_id)
        # Authorization at the DATA level: a customer can only see their own orders.
        # Return 404 (not 403) so we don't reveal that someone else's order id exists.
        if order is None or (actor.role is Role.CUSTOMER and order.customer_email != actor.email):
            raise NotFoundError(f"Order '{order_id}' not found", order_id=order_id)
        return order

    async def list_orders(self, flt: OrderFilter, *, limit: int, offset: int, actor: Principal) -> Page[OrderOut]:
        email = actor.email if actor.role is Role.CUSTOMER else flt.customer_email
        rows, total = await self.orders.list(status=flt.status.value if flt.status else None,
                                             customer_email=email, sort=flt.sort, limit=limit, offset=offset)
        return Page[OrderOut](items=[OrderOut.model_validate(o) for o in rows], total=total,
                              limit=limit, offset=offset)

    # ---------------------------------------------------------------- create
    async def create_order(self, data: OrderCreate, actor: Principal, idempotency_key: str | None = None) -> tuple[Order, bool]:
        """Returns (order, created). created=False means an idempotent replay."""
        email = data.customer_email.lower()
        if actor.role is Role.CUSTOMER and email != actor.email:
            raise ValidationFailed("Customers can only create orders for themselves")

        fingerprint = hashlib.sha256(data.model_dump_json().encode()).hexdigest()
        if idempotency_key:
            existing = await self.idempotency.find(idempotency_key, actor.user_id)
            if existing:
                if existing.request_hash != fingerprint:
                    # Same key, different body: client bug. Refuse rather than guess.
                    raise ConflictError("Idempotency-Key was already used with a different request")
                return await self.get_order(existing.order_id, actor), False

        order = Order(id=str(uuid.uuid4()), customer_email=email, status=OrderStatus.PENDING.value)
        with span("order.reserve_stock"):
            total = 0
            for line in data.items:
                product = await self.products.get_by_sku(line.sku)
                if product is None:
                    raise NotFoundError(f"Unknown SKU '{line.sku}'", sku=line.sku)
                if not await self.products.try_reserve(product.id, line.quantity):
                    raise InsufficientStockError(f"Not enough stock for {line.sku}", sku=line.sku)
                order.items.append(OrderItem(product_id=product.id, sku=product.sku, quantity=line.quantity,
                                             unit_price_cents=product.price_cents))
                total += product.price_cents * line.quantity
            order.total_cents = total
        await self.orders.add(order)

        with span("order.charge"):
            # If this raises, session_scope rolls back: stock reservation AND order row vanish together.
            result = await self.payment.charge(order_id=order.id, amount_cents=total, currency="usd",
                                               customer_email=email)
        if result.status is not PaymentStatus.SUCCEEDED:
            raise PaymentFailedError("Payment did not succeed")
        order.payment_ref, order.status = result.reference, OrderStatus.PAID.value

        await self._sync_crm(order)  # best effort, see method

        if idempotency_key:
            try:
                async with self.session.begin_nested():  # SAVEPOINT: a duplicate must not poison the whole txn
                    await self.idempotency.add(IdempotencyKey(key=idempotency_key, user_id=actor.user_id,
                                                              request_hash=fingerprint, order_id=order.id))
            except IntegrityError as exc:
                # Two identical requests raced; the other one won. Ask the client to retry -> replay.
                raise ConflictError("Concurrent request with the same Idempotency-Key; retry") from exc

        ORDERS_CREATED.labels("created").inc()
        log_event(log, "order_created", order_id=order.id, total_cents=total, items=len(order.items))
        return order, True

    async def _sync_crm(self, order: Order) -> None:
        """Graceful degradation: the CRM is nice-to-have. A CRM outage must NOT
        fail a paid order, so we swallow UpstreamUnavailableError and log it."""
        try:
            profile = await self.crm.upsert_customer(email=order.customer_email)
            order.crm_contact_id = profile.crm_id
        except UpstreamUnavailableError as exc:
            log_event(log, "crm_sync_skipped", order_id=order.id, reason=exc.message)

    # ---------------------------------------------------------------- update
    async def update_status(self, order_id: str, new: OrderStatus, actor: Principal) -> Order:
        if actor.role is Role.CUSTOMER and new is not OrderStatus.CANCELLED:
            raise ValidationFailed("Customers may only cancel their own orders")
        order = await self.get_order(order_id, actor)
        current = OrderStatus(order.status)
        if new == current:
            return order  # idempotent no-op: repeating the same update is harmless
        if new not in TRANSITIONS[current]:
            raise InvalidTransitionError(f"Cannot move order from '{current.value}' to '{new.value}'",
                                         current=current.value, requested=new.value)
        if new is OrderStatus.CANCELLED:
            await self._compensate_cancel(order, current)
        order.status = new.value
        await self.session.flush()
        log_event(log, "order_status_changed", order_id=order.id, old=current.value, new=new.value)
        return order

    async def _compensate_cancel(self, order: Order, current: OrderStatus) -> None:
        """Undo side effects: refund money (if paid) and return stock."""
        if current is OrderStatus.PAID and order.payment_ref:
            await self.payment.refund(payment_ref=order.payment_ref, amount_cents=order.total_cents)
        for item in order.items:
            await self.products.release(item.product_id, item.quantity)
