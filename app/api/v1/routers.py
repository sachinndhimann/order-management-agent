"""REST routers, version 1 (Chapter 1: routing, REST conventions, parameters).

REST CONVENTIONS used here
  noun URLs, plural collections      /orders, /orders/{id}
  GET    safe + idempotent           read
  POST   create (not idempotent)     201 + Location header (+ Idempotency-Key to make it so)
  PATCH  partial update              status change
  PUT    full replace, idempotent    (used on the CRM side)
  status codes: 200 OK, 201 Created, 204 No Content, 401/403 authn/authz, 404, 409 conflict, 422 validation

PARAMETER KINDS (all in create_order / list_orders below)
  path    /orders/{order_id}            identifies THE resource
  query   ?status=paid&limit=10         filters, paging, sorting
  header  Idempotency-Key               metadata about the request
  body    OrderCreate JSON              the resource representation
"""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import (
    CurrentUser, InventoryServiceDep, OrderServiceDep, PageDep, get_container, require_admin,
)
from app.container import Container
from app.core.errors import AuthenticationError, ConflictError, UpstreamUnavailableError
from app.core.observability import log_event
from app.core.security import Principal, Role, create_access_token, hash_password, verify_password
from app.db.models import OrderStatus, User
from app.repositories.repos import UserRepository
from app.schemas.common import Page
from app.schemas.orders import (
    EnrichedOrderOut, OrderCreate, OrderFilter, OrderOut, OrderStatusUpdate, ProductOut, StockOut, TokenOut, UserOut,
)
from app.db.session import session_scope

log = logging.getLogger("api")

# ------------------------------------------------------------------ auth
auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(email: Annotated[str, Query(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")],
                   password: Annotated[str, Query(min_length=8)],
                   c: Annotated[Container, Depends(get_container)]):
    """Self-service signup always creates a CUSTOMER; staff/admin are created out-of-band.
    (Credentials in the query string are for lab simplicity only - use a JSON body in real life.)"""
    async with session_scope(c.sessionmaker) as s:
        repo = UserRepository(s)
        if await repo.get_by_email(email):
            raise ConflictError("Email already registered")
        return await repo.add(User(email=email.lower(), password_hash=hash_password(password), role=Role.CUSTOMER.value))


@auth_router.post("/token", response_model=TokenOut)
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()],
                c: Annotated[Container, Depends(get_container)]):
    """OAuth2 password grant. `username` is the email. Same error for 'no such user'
    and 'bad password' so attackers can't enumerate accounts."""
    async with session_scope(c.sessionmaker) as s:
        user = await UserRepository(s).get_by_email(form.username)
    if not user or not verify_password(form.password, user.password_hash):
        raise AuthenticationError("Incorrect email or password")
    token = create_access_token(Principal(user.id, user.email, Role(user.role)), c.settings)
    return TokenOut(access_token=token, expires_in=c.settings.access_token_minutes * 60)


@auth_router.get("/me", response_model=UserOut)
async def me(user: CurrentUser, c: Annotated[Container, Depends(get_container)]):
    async with session_scope(c.sessionmaker) as s:
        return await UserRepository(s).get(user.user_id)


# ------------------------------------------------------------------ products / inventory
products_router = APIRouter(prefix="/products", tags=["inventory"])


@products_router.get("", response_model=Page[ProductOut])
async def list_products(page: PageDep, user: CurrentUser, c: Annotated[Container, Depends(get_container)],
                        in_stock: Annotated[bool, Query(description="Only products with stock > 0")] = False):
    from app.repositories.repos import ProductRepository
    async with session_scope(c.sessionmaker) as s:
        rows, total = await ProductRepository(s).list(limit=page.limit, offset=page.offset, in_stock_only=in_stock)
        return Page[ProductOut](items=[ProductOut.model_validate(r) for r in rows], total=total,
                                limit=page.limit, offset=page.offset)


@products_router.get("/{sku}/stock", response_model=StockOut)
async def check_stock(sku: Annotated[str, Path(min_length=2, max_length=64)], user: CurrentUser,
                      inventory: InventoryServiceDep):
    return await inventory.check_stock(sku)


# ------------------------------------------------------------------ orders
orders_router = APIRouter(prefix="/orders", tags=["orders"])


async def notify_customer(order_id: str, email: str) -> None:
    """BACKGROUND TASK: runs after the response is sent. Good for fire-and-forget
    work (email, webhooks). NOT durable: if the process dies it is lost - use a
    queue (Celery/RQ/SQS) when the work must not be lost."""
    await asyncio.sleep(0)  # stand-in for sending an email
    log_event(log, "customer_notified", order_id=order_id, to=email)


@orders_router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED,
                    responses={402: {"description": "Payment declined"}, 409: {"description": "Out of stock / idempotency conflict"}})
async def create_order(body: OrderCreate, response: Response, background: BackgroundTasks,
                       svc: OrderServiceDep, user: CurrentUser,
                       idempotency_key: Annotated[str | None, Header(min_length=8, max_length=128)] = None):
    order, created = await svc.create_order(body, user, idempotency_key)
    response.headers["Location"] = f"/api/v1/orders/{order.id}"
    if created:
        background.add_task(notify_customer, order.id, order.customer_email)
    else:
        response.status_code = status.HTTP_200_OK  # replay of an earlier success
        response.headers["Idempotent-Replay"] = "true"
    return order


@orders_router.get("", response_model=Page[OrderOut])
async def list_orders(svc: OrderServiceDep, user: CurrentUser, page: PageDep,
                      status_: Annotated[OrderStatus | None, Query(alias="status")] = None,
                      customer_email: str | None = None,
                      sort: Annotated[str, Query(pattern=r"^-?(created_at|total_cents|status)$")] = "-created_at"):
    return await svc.list_orders(OrderFilter(status=status_, customer_email=customer_email, sort=sort),
                                 limit=page.limit, offset=page.offset, actor=user)


@orders_router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: Annotated[str, Path(min_length=36, max_length=36)], svc: OrderServiceDep, user: CurrentUser):
    return await svc.get_order(order_id, user)


@orders_router.patch("/{order_id}/status", response_model=OrderOut)
async def update_status(order_id: str, body: OrderStatusUpdate, svc: OrderServiceDep, user: CurrentUser):
    return await svc.update_status(order_id, body.status, user)


@orders_router.get("/{order_id}/enriched", response_model=EnrichedOrderOut)
async def enriched_order(order_id: str, svc: OrderServiceDep, user: CurrentUser,
                         c: Annotated[Container, Depends(get_container)]):
    """ASYNC CONCURRENCY: CRM and shipping lookups are independent network calls.
    `await a; await b` costs t(a)+t(b); asyncio.gather costs max(t(a), t(b)).
    return_exceptions=True lets one failing enrichment degrade, not fail, the response."""
    order = await svc.get_order(order_id, user)
    crm_res, ship_res = await asyncio.gather(
        c.crm.get_customer(order.customer_email),
        c.shipping.quote(postal_code="94105", weight_kg=max(1.0, sum(i.quantity for i in order.items) * 0.5)),
        return_exceptions=True)
    degraded: list[str] = []
    tier = eta = None
    if isinstance(crm_res, UpstreamUnavailableError):
        degraded.append("crm")
    elif crm_res:
        tier = crm_res.tier
    if isinstance(ship_res, UpstreamUnavailableError):
        degraded.append("shipping")
    elif not isinstance(ship_res, BaseException):
        eta = ship_res.eta_days
    for r in (crm_res, ship_res):  # unexpected exception types are real bugs: re-raise
        if isinstance(r, BaseException) and not isinstance(r, UpstreamUnavailableError):
            raise r
    return EnrichedOrderOut(order=OrderOut.model_validate(order), customer_tier=tier,
                            shipping_eta_days=eta, degraded=degraded)


# ------------------------------------------------------------------ reports (admin only)
reports_router = APIRouter(prefix="/reports", tags=["reports"], dependencies=[Depends(require_admin)])
# `dependencies=[...]` on the router applies a guard to every route without
# adding a parameter to each function.


@reports_router.get("/sales")
async def sales(c: Annotated[Container, Depends(get_container)], days: Annotated[int, Query(ge=1, le=365)] = 30):
    return {"days": days, "rows": await c.reporting.sales_summary(days)}


@reports_router.get("/low-stock")
async def low_stock(c: Annotated[Container, Depends(get_container)], threshold: Annotated[int, Query(ge=0)] = 5):
    return {"threshold": threshold, "rows": await c.reporting.low_stock(threshold)}

