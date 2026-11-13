"""Chapter 3 - Backend architecture: layers, transactions, pagination, RBAC, idempotency, state machine, observability."""
import asyncio
import logging

import pytest
from sqlalchemy import select

from app.core.errors import (
    ConflictError, InsufficientStockError, InvalidTransitionError, NotFoundError, ValidationFailed,
)
from app.core.observability import JsonFormatter, log_event, request_id_var
from app.core.security import (
    Principal, Role, create_access_token, decode_access_token, hash_password, verify_password,
)
from app.db.models import Order, OrderStatus, Product
from app.db.session import session_scope
from app.repositories.repos import ProductRepository
from app.schemas.orders import OrderCreate, OrderFilter, OrderItemIn
from app.services.factory import build_order_service

STAFF = Principal(2, "staff@example.com", Role.STAFF)
ADA = Principal(3, "ada@example.com", Role.CUSTOMER)


def order_data(sku="KB-100", qty=1, email="ada@example.com"):
    return OrderCreate(customer_email=email, items=[OrderItemIn(sku=sku, quantity=qty)])


async def svc_call(container, fn):
    """Run fn(service) in its own unit of work, exactly like a request/tool call."""
    async with session_scope(container.sessionmaker) as s:
        return await fn(build_order_service(s, container))


# ---------------------------------------------------------------- service layer (no HTTP at all)
async def test_service_works_without_http_or_mcp(container):
    order, created = await svc_call(container, lambda s: s.create_order(order_data(qty=3), STAFF))
    assert created and order.status == "paid" and order.total_cents == 3 * 8999
    assert order.payment_ref == "ch_1" and order.crm_contact_id == "c-1"


async def test_repository_atomic_reserve_prevents_overselling(file_container):
    """Two buyers race for the last 2 units of HD-400: exactly one wins."""
    async def buy():
        try:
            return await svc_call(file_container, lambda s: s.create_order(order_data("HD-400", 2), STAFF))
        except InsufficientStockError:
            return None
    results = await asyncio.gather(buy(), buy())
    assert sum(r is not None for r in results) == 1
    async with session_scope(file_container.sessionmaker) as s:
        assert (await ProductRepository(s).get_by_sku("HD-400")).stock == 0


async def test_transaction_rollback_on_payment_failure_leaves_no_trace(container):
    container.payment.decline = True
    with pytest.raises(Exception):
        await svc_call(container, lambda s: s.create_order(order_data(qty=5), STAFF))
    async with session_scope(container.sessionmaker) as s:
        assert (await ProductRepository(s).get_by_sku("KB-100")).stock == 25
        assert (await s.execute(select(Order))).first() is None


async def test_crm_outage_does_not_fail_order(container):
    container.crm.down = True
    order, _ = await svc_call(container, lambda s: s.create_order(order_data(), STAFF))
    assert order.status == "paid" and order.crm_contact_id is None


# ---------------------------------------------------------------- idempotency
async def test_idempotency_replay_returns_same_order_and_charges_once(container):
    a, c1 = await svc_call(container, lambda s: s.create_order(order_data(), STAFF, "retry-key-001"))
    b, c2 = await svc_call(container, lambda s: s.create_order(order_data(), STAFF, "retry-key-001"))
    assert (c1, c2) == (True, False) and a.id == b.id
    assert len(container.payment.charges) == 1


async def test_idempotency_key_reuse_with_different_body_conflicts(container):
    await svc_call(container, lambda s: s.create_order(order_data(qty=1), STAFF, "retry-key-002"))
    with pytest.raises(ConflictError):
        await svc_call(container, lambda s: s.create_order(order_data(qty=2), STAFF, "retry-key-002"))


async def test_idempotent_http_replay_status_codes(client, auth):
    body = {"customer_email": "ada@example.com", "items": [{"sku": "MS-200", "quantity": 1}]}
    h = {**auth(), "Idempotency-Key": "http-key-0001"}
    first = await client.post("/api/v1/orders", json=body, headers=h)
    second = await client.post("/api/v1/orders", json=body, headers=h)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.headers["Idempotent-Replay"] == "true" and first.json()["id"] == second.json()["id"]


# ---------------------------------------------------------------- state machine + compensation
async def test_status_transitions_and_cancel_refunds_and_restocks(container):
    o, _ = await svc_call(container, lambda s: s.create_order(order_data(qty=2), STAFF))
    shipped = await svc_call(container, lambda s: s.update_status(o.id, OrderStatus.SHIPPED, STAFF))
    assert shipped.status == "shipped"
    with pytest.raises(InvalidTransitionError):
        await svc_call(container, lambda s: s.update_status(o.id, OrderStatus.CANCELLED, STAFF))  # too late

    o2, _ = await svc_call(container, lambda s: s.create_order(order_data(qty=2), STAFF))
    await svc_call(container, lambda s: s.update_status(o2.id, OrderStatus.CANCELLED, STAFF))
    assert container.payment.refunds == [{"ref": o2.payment_ref, "amount": o2.total_cents}]
    async with session_scope(container.sessionmaker) as s:
        assert (await ProductRepository(s).get_by_sku("KB-100")).stock == 25 - 2  # only o (shipped) still holds stock


async def test_same_status_update_is_idempotent_noop(container):
    o, _ = await svc_call(container, lambda s: s.create_order(order_data(), STAFF))
    again = await svc_call(container, lambda s: s.update_status(o.id, OrderStatus.PAID, STAFF))
    assert again.status == "paid"


# ---------------------------------------------------------------- RBAC / data-level authorization
async def test_customer_cannot_see_or_create_for_others(container):
    o, _ = await svc_call(container, lambda s: s.create_order(order_data(), STAFF))
    eve = Principal(9, "eve@example.com", Role.CUSTOMER)
    with pytest.raises(NotFoundError):  # 404, not 403: don't confirm existence
        await svc_call(container, lambda s: s.get_order(o.id, eve))
    with pytest.raises(ValidationFailed):
        await svc_call(container, lambda s: s.create_order(order_data(email="ada@example.com"), eve))
    page = await svc_call(container, lambda s: s.list_orders(OrderFilter(), limit=10, offset=0, actor=eve))
    assert page.total == 0
    mine = await svc_call(container, lambda s: s.list_orders(OrderFilter(), limit=10, offset=0, actor=ADA))
    assert mine.total == 1


async def test_customer_may_only_cancel(container):
    o, _ = await svc_call(container, lambda s: s.create_order(order_data(), ADA))
    with pytest.raises(ValidationFailed):
        await svc_call(container, lambda s: s.update_status(o.id, OrderStatus.SHIPPED, ADA))
    done = await svc_call(container, lambda s: s.update_status(o.id, OrderStatus.CANCELLED, ADA))
    assert done.status == "cancelled"


# ---------------------------------------------------------------- pagination / filtering / sorting
async def test_pagination_filter_sort(container):
    for qty in (1, 2, 3):
        await svc_call(container, lambda s, q=qty: s.create_order(order_data("MS-200", q), STAFF))
    page1 = await svc_call(container, lambda s: s.list_orders(OrderFilter(sort="-total_cents"), limit=2, offset=0, actor=STAFF))
    assert page1.total == 3 and [o.total_cents for o in page1.items] == [3 * 2999, 2 * 2999] and page1.has_more
    page2 = await svc_call(container, lambda s: s.list_orders(OrderFilter(sort="-total_cents"), limit=2, offset=2, actor=STAFF))
    assert [o.total_cents for o in page2.items] == [2999] and not page2.has_more
    none = await svc_call(container, lambda s: s.list_orders(OrderFilter(status=OrderStatus.SHIPPED), limit=5, offset=0, actor=STAFF))
    assert none.total == 0


async def test_sort_whitelist_blocks_injection(client, auth):
    r = await client.get("/api/v1/orders?sort=id;DROP TABLE orders", headers=auth())
    assert r.status_code == 422


# ---------------------------------------------------------------- DB constraints / pooling config
async def test_db_check_constraint_is_last_line_of_defence(container):
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        async with session_scope(container.sessionmaker) as s:
            p = await ProductRepository(s).get_by_sku("HD-400")
            p.stock = -1
            await s.flush()


def test_pool_settings_applied_for_postgres():
    from app.core.config import Settings
    from app.db.session import make_engine
    e = make_engine(Settings(database_url="postgresql+asyncpg://u:p@localhost/db", db_pool_size=7, db_max_overflow=3, _env_file=None))
    assert e.pool.size() == 7 and e.pool._max_overflow == 3


# ---------------------------------------------------------------- security primitives
def test_password_hashing_and_jwt(settings):
    h = hash_password("s3cret-pass")
    assert h != "s3cret-pass" and verify_password("s3cret-pass", h) and not verify_password("wrong", h)
    tok = create_access_token(STAFF, settings)
    assert decode_access_token(tok, settings) == STAFF
    expired = create_access_token(STAFF, settings, expires_in=-10)
    with pytest.raises(Exception):
        decode_access_token(expired, settings)


# ---------------------------------------------------------------- observability
def test_json_logs_carry_request_id():
    rec = logging.LogRecord("t", logging.INFO, "", 0, "hello", None, None)
    rec.extra_fields = {"order_id": "o1"}
    tok = request_id_var.set("req-42")
    try:
        import json
        out = json.loads(JsonFormatter().format(rec))
    finally:
        request_id_var.reset(tok)
    assert out["request_id"] == "req-42" and out["order_id"] == "o1" and out["msg"] == "hello"
    assert log_event  # helper importable


async def test_product_seed_is_idempotent(container):
    from app.seed import seed
    await seed(container.sessionmaker)
    async with session_scope(container.sessionmaker) as s:
        assert len((await s.execute(select(Product))).scalars().all()) == 6
