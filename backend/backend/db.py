"""Async SQLAlchemy engine + session factory.

The default database is a SQLite file at ``backend/data/argus.db``. The env
var ``ARGUS_DB_URL`` overrides the default (matches the rest of the
``ARGUS_*`` config namespace documented in README / docs / config.py).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent
DATA_DIR = BACKEND_DIR / "data"


def _default_sqlite_url() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{DATA_DIR / 'argus.db'}"


def _resolve_database_url() -> str:
    """Pick the DB URL: ``ARGUS_DB_URL`` env var or the SQLite default."""
    primary = os.environ.get("ARGUS_DB_URL")
    if primary:
        return primary
    return _default_sqlite_url()


DATABASE_URL: str = _resolve_database_url()


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _pool_defaults_for(url: str) -> dict:
    """Return pool sizing defaults sized to the URL's backend.

    The monitor serves a mixed workload — SSE streams hold a connection
    for the whole subscription, event ingest is chatty, and the
    dashboard/projects pages fan out to 10+ sub-queries per request.
    The stock SQLAlchemy async default (``pool_size=5``,
    ``max_overflow=10``, ``pool_timeout=30``) is undersized for this
    and triggers ``QueuePool limit of size 5 overflow 10 reached``
    under live traffic.

    * SQLite (file or ``:memory:``): a larger pool doesn't help because
      the DB is single-writer. Keep things compact; ``:memory:`` still
      uses ``StaticPool`` elsewhere.
    * Postgres / other dialects: use a 20/30 pool with recycle+pre-ping
      so idle connections don't get killed by a firewall / pgbouncer.

    Operators override via ``ARGUS_DB_POOL_SIZE`` /
    ``ARGUS_DB_POOL_MAX_OVERFLOW`` / ``ARGUS_DB_POOL_TIMEOUT`` /
    ``ARGUS_DB_POOL_RECYCLE``.
    """
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        defaults = {
            "pool_size": 5,
            "max_overflow": 10,
            "pool_timeout": 30,
        }
    else:
        defaults = {
            "pool_size": 20,
            "max_overflow": 30,
            "pool_timeout": 30,
            "pool_recycle": 1800,
            "pool_pre_ping": True,
        }
    # Env overrides (strings → ints where appropriate).
    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            log.warning("invalid %s=%r; falling back to %d", name, raw, default)
            return default

    defaults["pool_size"] = _int_env(
        "ARGUS_DB_POOL_SIZE", defaults["pool_size"]
    )
    defaults["max_overflow"] = _int_env(
        "ARGUS_DB_POOL_MAX_OVERFLOW", defaults["max_overflow"]
    )
    defaults["pool_timeout"] = _int_env(
        "ARGUS_DB_POOL_TIMEOUT", defaults["pool_timeout"]
    )
    if not is_sqlite:
        defaults["pool_recycle"] = _int_env(
            "ARGUS_DB_POOL_RECYCLE", defaults["pool_recycle"]
        )
    return defaults


# A single engine per process. ``connect_args`` is sqlite-specific but is
# harmless on other backends because we only ever use SQLite here.
_engine_kwargs: dict = {"future": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
elif "postgresql" in DATABASE_URL:
    # Disable SSL on LAN Postgres deployments by default — asyncpg's SSL
    # negotiation adds 20-30s on cold connections when the server isn't
    # configured for TLS, and LAN deployments (the typical self-hosted
    # case) don't need it. Users on the public internet should set
    # ARGUS_DB_SSL=require.
    ssl_mode = os.environ.get("ARGUS_DB_SSL", "disable").lower()
    if ssl_mode == "disable":
        _engine_kwargs["connect_args"] = {"ssl": False}
    # else: let asyncpg negotiate ssl automatically from the URL.
    # ``:memory:`` databases only live for the connection that created them,
    # so for tests we force a single shared connection via StaticPool.
    if ":memory:" in DATABASE_URL:
        _engine_kwargs["poolclass"] = StaticPool
    else:
        _engine_kwargs.update(_pool_defaults_for(DATABASE_URL))
else:
    _engine_kwargs.update(_pool_defaults_for(DATABASE_URL))

engine: AsyncEngine = create_async_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def init_db() -> None:
    """Create tables if they don't exist yet."""
    # Importing here ensures models are registered against Base before
    # ``create_all`` runs.
    from backend import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("database initialised at %s", DATABASE_URL)


async def dispose_db() -> None:
    """Dispose the engine (used during shutdown and test teardown)."""
    await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields an ``AsyncSession`` per request."""
    async with SessionLocal() as session:
        yield session
