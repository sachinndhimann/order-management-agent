"""PostgreSQL connector (Chapter 6): a read-only reporting adapter.

Why a SECOND way to reach Postgres when we have the ORM? Different job,
different trust level: reporting uses a separate read-only database role
(REPORTING_DATABASE_URL), its own pool, a statement timeout, and only a fixed
menu of parameterised queries. It is NOT "run any SQL the LLM writes" -
that is the classic text-to-SQL injection hole.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings

# The whole menu. Parameters are bound (:days), never string-formatted.
QUERIES = {
    "sales_by_status": text(
        "SELECT status, COUNT(*) AS orders, COALESCE(SUM(total_cents), 0) AS revenue_cents "
        "FROM orders WHERE created_at >= :since GROUP BY status ORDER BY status"
    ),
    "low_stock": text("SELECT sku, name, stock FROM products WHERE stock <= :threshold ORDER BY stock, sku"),
}


class ReportingConnector:
    def __init__(self, engine: AsyncEngine, *, owns_engine: bool = False, statement_timeout_ms: int = 3000):
        self._engine, self._owns, self._timeout_ms = engine, owns_engine, statement_timeout_ms

    @classmethod
    def from_settings(cls, s: Settings, *, fallback_engine: AsyncEngine) -> ReportingConnector:
        if s.reporting_database_url:
            engine = create_async_engine(s.reporting_database_url.get_secret_value(), pool_size=2,
                                         max_overflow=2, pool_pre_ping=True)
            return cls(engine, owns_engine=True)
        return cls(fallback_engine)  # dev/test: share the main engine

    async def run(self, name: str, **params) -> list[dict]:
        query = QUERIES[name]  # KeyError => unknown report; no arbitrary SQL possible
        async with self._engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                # LOCAL = scoped to this transaction; protects the DB from runaway queries.
                await conn.execute(text(f"SET LOCAL statement_timeout = {int(self._timeout_ms)}"))
            result = await conn.execute(query, params)
            return [dict(row._mapping) for row in result]

    async def sales_summary(self, days: int = 30) -> list[dict]:
        from datetime import UTC, datetime, timedelta
        return await self.run("sales_by_status", since=datetime.now(UTC) - timedelta(days=days))

    async def low_stock(self, threshold: int = 5) -> list[dict]:
        return await self.run("low_stock", threshold=threshold)

    async def aclose(self) -> None:
        if self._owns:
            await self._engine.dispose()
