"""Engine, connection pool and the unit-of-work context manager (Chapter 3).

CONNECTION POOLING: opening a Postgres connection costs ~ms (TCP + TLS + auth).
The pool keeps `pool_size` connections open and lends them out; `max_overflow`
allows short bursts; `pool_pre_ping` detects connections the server dropped.
Total possible connections = (pool_size + max_overflow) * number_of_processes —
this must stay below Postgres `max_connections` (default 100)!

TRANSACTIONS: a request == one unit of work == one transaction. `session_scope`
commits if the block finishes and rolls back on ANY exception, so a half-created
order (stock reserved but payment failed) can never be persisted.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    url = settings.database_url
    if url.startswith("sqlite"):
        # SQLite has no real pool; in-memory DBs need StaticPool so every
        # session sees the same database (used by tests).
        kwargs = {"poolclass": StaticPool} if ":memory:" in url else {}
        return create_async_engine(url, connect_args={"check_same_thread": False}, **kwargs)
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit (needed to
    # serialize the response after the dependency has committed).
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
