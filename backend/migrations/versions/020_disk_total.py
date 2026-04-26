"""Disk total propagation: add ``resource_snapshot.disk_total_mb``.

Revision ID: 020_disk_total
Revises: 018_perf_indexes
Create Date: 2026-04-25 00:00:00

The DeepTS reporter (``scripts/common/resource_snapshot.py``) was updated
to emit ``disk_total_mb`` alongside the long-standing ``disk_free_mb``.
With both fields available the frontend can render a real
"disk fullness" bar (``used% = (total - free) / total``) on the host
capacity chip — until now the bar fell back to a free-GB pressure
heuristic that didn't match what operators see in ``df -h``.

Schema change is additive and nullable:

* New column ``resource_snapshot.disk_total_mb`` (REAL/FLOAT, NULL).
* No default — existing rows stay NULL until a fresh snapshot from the
  updated reporter overwrites them. The frontend treats NULL as
  "unknown" and falls back to the legacy free-GB heuristic.
* No index needed — column is read alongside the row, never queried in
  isolation.

Downgrade drops the column. Postgres handles ``DROP COLUMN`` natively;
SQLite gets a ``batch_alter_table`` rebuild.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "020_disk_total"
down_revision: Union[str, Sequence[str], None] = "018_perf_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("resource_snapshot") as batch_op:
        batch_op.add_column(
            sa.Column("disk_total_mb", sa.Float(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("resource_snapshot") as batch_op:
        batch_op.drop_column("disk_total_mb")
