"""Data-retention sweeper for the Argus DB.

Runs periodically (driven by the lifespan loop in ``app.py``) to delete
rows that are older than the configured caps.

Design choices
--------------
* Each DELETE is a separate statement executed in the caller's session.
  The background loop opens one session per sweep and commits at the end.
  The HTTP endpoint shares the request session and the router commits on
  return — both paths work without nested transactions or savepoints.
* If one rule raises an exception it is caught, logged, and recorded in
  the returned stats as -1; the remaining rules still execute.

Tables that are **never** purged:
  - ``batch`` — result archives; callers soft-delete via ``is_deleted``
  - ``job``   — ditto
  TODO: add ARGUS_RETENTION_BATCH_DAYS when an archiving policy is agreed.

Demo-host snapshots use the shorter ``retention_demo_data_days`` cap so
the live demo fixture does not consume unbounded disk over time while still
being refreshed promptly.

DB-dialect notes
----------------
Timestamps are stored as ISO 8601 TEXT columns (``"2026-04-23T09:23:06Z"``).
Both SQLite and Postgres support comparison of ISO 8601 strings with a plain
``< datetime_expression`` predicate because the lexicographic sort of the
format equals the chronological sort.

We use ``sqlalchemy.func.datetime`` with a signed offset string so the
expression works for SQLite (dev / prod):

    ``datetime('now', '-7 days')`` → TEXT comparison vs TEXT column

TODO: For a Postgres migration, replace ``_cutoff_expr`` with a dialect
guard: ``now() - INTERVAL '7 days'`` for Postgres,
``datetime('now', '-7 days')`` for SQLite.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.demo.seed import DEMO_HOST
from backend.models import Event, ResourceSnapshot

log = logging.getLogger(__name__)


def _cutoff_expr(days: int):
    """Return a SQLAlchemy expression for ``now() - <days> days``.

    Uses SQLite-compatible ``datetime('now', '-N days')`` syntax.
    Compares correctly with ISO 8601 TEXT timestamps because their
    lexicographic and chronological orderings are identical.
    """
    return func.datetime("now", f"-{days} days")


async def sweep_once(db: AsyncSession, settings: Settings) -> dict[str, int]:
    """Delete rows older than the configured retention caps.

    Executes each DELETE within the session's current transaction. If a
    rule fails the exception is logged and the remaining rules still run.

    Returns a dict mapping ``"table.criterion"`` to deleted row count.
    A value of -1 indicates that rule failed with an error.
    """
    stats: dict[str, int] = {}

    rules = []

    # 1. resource_snapshot — normal (non-demo) hosts
    if settings.retention_snapshot_days > 0:
        rules.append((
            "resource_snapshot.normal",
            delete(ResourceSnapshot)
            .where(
                ResourceSnapshot.timestamp
                < _cutoff_expr(settings.retention_snapshot_days)
            )
            .where(ResourceSnapshot.host != DEMO_HOST),
        ))

    # 2. resource_snapshot — demo host (shorter retention cap)
    if settings.retention_demo_data_days > 0:
        rules.append((
            "resource_snapshot.demo",
            delete(ResourceSnapshot)
            .where(
                ResourceSnapshot.timestamp
                < _cutoff_expr(settings.retention_demo_data_days)
            )
            .where(ResourceSnapshot.host == DEMO_HOST),
        ))

    # 3. event — log_line
    if settings.retention_log_line_days > 0:
        rules.append((
            "event.log_line",
            delete(Event)
            .where(Event.event_type == "log_line")
            .where(
                Event.timestamp < _cutoff_expr(settings.retention_log_line_days)
            ),
        ))

    # 4. event — job_epoch
    if settings.retention_job_epoch_days > 0:
        rules.append((
            "event.job_epoch",
            delete(Event)
            .where(Event.event_type == "job_epoch")
            .where(
                Event.timestamp < _cutoff_expr(settings.retention_job_epoch_days)
            ),
        ))

    # 5. event — everything else (batch_start/done, job_start/done, …)
    if settings.retention_event_other_days > 0:
        rules.append((
            "event.other",
            delete(Event)
            .where(Event.event_type.not_in(["log_line", "job_epoch"]))
            .where(
                Event.timestamp < _cutoff_expr(settings.retention_event_other_days)
            ),
        ))

    for key, stmt in rules:
        try:
            result = await db.execute(stmt)
            n = result.rowcount
            stats[key] = n
            if n:
                log.info(
                    "retention.sweep_once: deleted %d from %s", n, key
                )
        except Exception:  # noqa: BLE001
            log.exception("retention.sweep_once: rule %s failed", key)
            stats[key] = -1  # sentinel: rule error

    return stats
