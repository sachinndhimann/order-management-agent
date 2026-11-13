"""Shared DTOs: pagination envelope, sort/filter params (Chapter 3).

DTO (Data Transfer Object) = the shape that crosses a boundary. We never return
ORM objects directly: that would leak columns (password_hash!), couple clients
to the DB schema and break on lazy-loading. Pydantic does validation on the way
in and serialization on the way out.
"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageParams(BaseModel):
    """Offset pagination. Simple + supports 'jump to page N'. For huge tables
    prefer keyset (cursor) pagination: OFFSET N still scans N rows."""

    limit: int = Field(20, ge=1, le=100, description="Page size (max 100)")
    offset: int = Field(0, ge=0)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total
