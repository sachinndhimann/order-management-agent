"""SQLAlchemy 2.0 ORM models (Chapter 3).

ORM = map Python classes to tables so the *repository* layer can work with
objects. Money is stored as integer cents: floats cannot represent 0.10 exactly.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class OrderStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="customer")


class Product(Base):
    __tablename__ = "products"
    # CHECK constraint = last line of defence: even buggy code can't oversell.
    __table_args__ = (CheckConstraint("stock >= 0", name="ck_products_stock_nonneg"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    price_cents: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer, default=0)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_status_created", "status", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4 str, created by service
    customer_email: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.PENDING.value)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    payment_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    crm_contact_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    # lazy="selectin": load items with ONE extra query; avoids lazy IO in async (MissingGreenlet).
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    sku: Mapped[str] = mapped_column(String(64))  # snapshot: catalogue may change later
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)  # snapshot of price at purchase time
    order: Mapped[Order] = relationship(back_populates="items")


class IdempotencyKey(Base):
    """Remember (key -> request fingerprint -> order) so retries don't duplicate orders."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("key", "user_id", name="uq_idem_key_user"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
