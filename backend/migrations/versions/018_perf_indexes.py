"""Team Perf: hot-path indexes for dashboard / batches / jobs / events.

Revision ID: 018_perf_indexes
Revises: 103385a6a1d0
Create Date: 2026-04-25 00:00:00

Adds the indexes that the benchmark in ``backend/benchmark_hot_pages.py``
identified as missing on the hot read paths:

* ``job.batch_id`` — the Job PK is ``(id, batch_id)`` with ``id``
  leading, so the auto-index doesn't serve ``WHERE batch_id = ?``.
  Every /api/batches/{id}/jobs call scans. Add a trio of composites.
* ``event(batch_id, job_id, event_type, timestamp)`` — epoch timeseries
  and eta-all lookups filter on all four columns.
* ``event(batch_id, event_type, timestamp)`` — activity feed +
  notifications; avoids scanning batch_id + NULL job_id rows.
* ``batch(start_time)`` + ``batch(status, start_time)`` +
  ``batch(project, start_time)`` + ``batch(owner_id, status)`` —
  /api/batches default ordering, dashboard "running" counters,
  project list grouping, per-user "my_running" tile.
* ``resource_snapshot(timestamp)`` — dashboard's "active hosts in the
  last 5 minutes" sweep.

All 10 indexes are **additive** — creation is idempotent via
``CREATE INDEX IF NOT EXISTS`` which SQLite (3.8+) and Postgres (9.5+)
both support natively. No column or type changes; no row rewrites;
safe to apply online.

Downgrade drops every index added here.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "018_perf_indexes"
down_revision: Union[str, Sequence[str], None] = "103385a6a1d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (index_name, create_sql) pairs. Using raw SQL with ``IF NOT EXISTS``
# so the migration is re-runnable and portable across SQLite + Postgres.
_INDEXES: list[tuple[str, str]] = [
    # --- job --------------------------------------------------------
    (
        "idx_job_batch",
        "CREATE INDEX IF NOT EXISTS idx_job_batch ON job (batch_id)",
    ),
    (
        "idx_job_batch_status",
        "CREATE INDEX IF NOT EXISTS idx_job_batch_status "
        "ON job (batch_id, status)",
    ),
    (
        "idx_job_batch_status_end",
        "CREATE INDEX IF NOT EXISTS idx_job_batch_status_end "
        "ON job (batch_id, status, end_time)",
    ),
    # --- event ------------------------------------------------------
    (
        "idx_event_batch_job_type_ts",
        "CREATE INDEX IF NOT EXISTS idx_event_batch_job_type_ts "
        "ON event (batch_id, job_id, event_type, timestamp)",
    ),
    (
        "idx_event_batch_type_ts",
        "CREATE INDEX IF NOT EXISTS idx_event_batch_type_ts "
        "ON event (batch_id, event_type, timestamp)",
    ),
    # --- batch ------------------------------------------------------
    (
        "idx_batch_start_time",
        "CREATE INDEX IF NOT EXISTS idx_batch_start_time "
        "ON batch (start_time)",
    ),
    (
        "idx_batch_status_start",
        "CREATE INDEX IF NOT EXISTS idx_batch_status_start "
        "ON batch (status, start_time)",
    ),
    (
        "idx_batch_project_start",
        "CREATE INDEX IF NOT EXISTS idx_batch_project_start "
        "ON batch (project, start_time)",
    ),
    (
        "idx_batch_owner_status",
        "CREATE INDEX IF NOT EXISTS idx_batch_owner_status "
        "ON batch (owner_id, status)",
    ),
    # --- resource_snapshot ------------------------------------------
    (
        "idx_resource_timestamp",
        "CREATE INDEX IF NOT EXISTS idx_resource_timestamp "
        "ON resource_snapshot (timestamp)",
    ),
]


def upgrade() -> None:
    for _name, sql in _INDEXES:
        op.execute(sql)


def downgrade() -> None:
    for name, _sql in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
