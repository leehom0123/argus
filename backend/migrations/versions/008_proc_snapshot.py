"""Add proc_* columns + batch_id index to resource_snapshot.

Revision ID: 008_proc_snapshot
Revises: 007_github_oauth
Create Date: 2026-04-24 00:00:02

New columns on ``resource_snapshot``:

* ``proc_cpu_pct``    (Float, nullable) — CPU share consumed by the
  reporting process (0-100).
* ``proc_ram_mb``     (Integer, nullable) — RSS in MB of the reporting
  process.
* ``proc_gpu_mem_mb`` (Integer, nullable) — VRAM in MB used by the
  reporting process (matched by PID via pynvml).
* ``batch_id``        (String 256, nullable, indexed) — ties a snapshot
  to the batch that was running when it was sampled.  Reporter clients
  send this as a top-level event envelope field; the ingest handler
  copies it here so the ``/resources`` endpoint can filter by batch.

Downgrade drops the 4 columns and the ``batch_id`` index.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008_proc_snapshot"
down_revision: Union[str, Sequence[str], None] = "007_github_oauth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("resource_snapshot") as rs_op:
        rs_op.add_column(sa.Column("proc_cpu_pct", sa.Float(), nullable=True))
        rs_op.add_column(sa.Column("proc_ram_mb", sa.Integer(), nullable=True))
        rs_op.add_column(sa.Column("proc_gpu_mem_mb", sa.Integer(), nullable=True))
        rs_op.add_column(
            sa.Column("batch_id", sa.String(256), nullable=True)
        )

    op.create_index(
        "idx_resource_snapshot_batch_id",
        "resource_snapshot",
        ["batch_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_resource_snapshot_batch_id", table_name="resource_snapshot")
    with op.batch_alter_table("resource_snapshot") as rs_op:
        rs_op.drop_column("batch_id")
        rs_op.drop_column("proc_gpu_mem_mb")
        rs_op.drop_column("proc_ram_mb")
        rs_op.drop_column("proc_cpu_pct")
