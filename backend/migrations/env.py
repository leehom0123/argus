"""Alembic environment wired for async SQLAlchemy.

Following the async pattern documented at
https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic
so `alembic upgrade head` works against the same ``sqlite+aiosqlite``
engine the application uses.

DB URL resolution order:
1. ``-x url=...`` on the alembic CLI
2. ``ARGUS_DB_URL`` env var
3. ``backend.config.get_settings().db_url`` default
"""
from __future__ import annotations

import asyncio
import logging.config
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# ---------------------------------------------------------------------------
# Import application metadata so autogenerate can diff against live tables.
# ---------------------------------------------------------------------------

from backend.db import Base  # noqa: E402
from backend import models  # noqa: E402, F401  -- ensure model side-effects

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # pragma: no cover - noisy CI, non-critical
        pass

target_metadata = Base.metadata


def _resolve_db_url() -> str:
    # CLI -x arg wins so CI can point at an ephemeral DB.
    xargs = context.get_x_argument(as_dictionary=True)
    if "url" in xargs:
        return xargs["url"]
    value = os.environ.get("ARGUS_DB_URL")
    if value:
        return value
    # Final fallback: the application's own default.
    from backend.config import get_settings

    return get_settings().db_url


DB_URL = _resolve_db_url()
config.set_main_option("sqlalchemy.url", DB_URL)


# ---------------------------------------------------------------------------
# Offline / online entrypoints
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=DB_URL.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=DB_URL.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = DB_URL
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
