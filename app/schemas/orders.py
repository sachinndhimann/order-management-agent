"""Order/product/auth DTOs (Pydantic v2) - used by REST *and* MCP tools.

Because MCP derives each tool's JSON Schema from these same models, ONE
definition validates REST bodies, documents OpenAPI AND tells the LLM how to
call the tool (Chapter 4/5: JSON Schema & structured output).
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from app.db.models import OrderStatus

Sku = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=64)]


class OrderItemIn(BaseModel):
    sku: Sku = Field(description="Product SKU, e.g. KB-100")
    quantity: int = Field(ge=1, le=100, description="Units to buy (1-100)")


class OrderCreate(BaseModel):
    customer_email: EmailStr
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)

    @field_validator("items")
    @classmethod
    def no_duplicate_skus(cls, items: list[OrderItemIn]) -> list[OrderItemIn]:
        skus = [i.sku for i in items]
        if len(skus) != len(set(skus)):
            raise ValueError("duplicate SKUs: merge quantities into one line")
        return items

    model_config = {"json_schema_extra": {"examples": [
        {"customer_email": "ada@example.com", "items": [{"sku": "KB-100", "quantity": 2}]}]}}


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # build from ORM objects
    sku: str
    quantity: int
    unit_price_cents: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    customer_email: str
    status: OrderStatus
    total_cents: int
    payment_ref: str | None = None
    created_at: datetime
    items: list[OrderItemOut]


class OrderStatusUpdate(BaseModel):
    status: OrderStatus


class OrderFilter(BaseModel):
    status: OrderStatus | None = None
    customer_email: str | None = None
    sort: str = Field("-created_at", pattern=r"^-?(created_at|total_cents|status)$",
                      description="Field to sort by; prefix with '-' for descending")


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sku: str
    name: str
    price_cents: int
    stock: int


class StockOut(BaseModel):
    sku: str
    name: str
    available: int
    in_stock: bool


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    role: str  # NOTE: no password_hash field => it can never be serialized


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class EnrichedOrderOut(BaseModel):
    order: OrderOut
    customer_tier: str | None = None
    shipping_eta_days: int | None = None
    degraded: list[str] = []  # which optional enrichments failed (graceful degradation)
