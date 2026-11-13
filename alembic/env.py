"""Alembic environment (Chapter 3: migrations).

WHY MIGRATIONS: `Base.metadata.create_all()` can only create missing tables; it
can't add a column to a table that already holds production data. Alembic keeps
an ordered, versioned history of schema changes (`alembic_version` table), so
every environment can upgrade - or roll back - deterministically and reviewably.

Workflow:  edit models -> `alembic revision --autogenerate -m "add x"` -> REVIEW the
file (autogenerate misses renames/data moves) -> `alembic upgrade head`.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata  # what autogenerate diffs against
URL = get_settings().database_url


def run_migrations_offline() -> None:  # emits SQL without a DB connection (for DBAs)
    context.configure(url=URL, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    # render_as_batch: SQLite can't ALTER most things; batch mode rebuilds the table.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(URL)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
