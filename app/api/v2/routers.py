"""API v2 (Chapter 1: versioning).

Versioning strategy: URL prefix (/api/v1, /api/v2). Pros: visible, cacheable,
easy to route and to document separately. Cons: URL churn. Alternatives: a
header (`Accept: application/vnd.shop.v2+json`) - cleaner URLs, harder to test
in a browser. RULE: additive changes (new optional fields) never need a new
version; BREAKING changes (rename/remove/semantics) do. v1 keeps working.

v2 change demonstrated: money as a structured object + HATEOAS-style links,
instead of v1's bare `total_cents`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path
from pydantic import BaseModel

from app.api.deps import CurrentUser, OrderServiceDep

router = APIRouter(prefix="/orders", tags=["orders (v2)"])


class Money(BaseModel):
    amount_cents: int
    currency: str = "USD"


class OrderV2(BaseModel):
    id: str
    status: str
    total: Money
    links: dict[str, str]


@router.get("/{order_id}", response_model=OrderV2)
async def get_order_v2(order_id: Annotated[str, Path(min_length=36, max_length=36)],
                       svc: OrderServiceDep, user: CurrentUser):
    o = await svc.get_order(order_id, user)
    return OrderV2(id=o.id, status=o.status, total=Money(amount_cents=o.total_cents),
                   links={"self": f"/api/v2/orders/{o.id}", "enriched": f"/api/v1/orders/{o.id}/enriched"})
