"""Baseline tables: batch, job, event, resource_snapshot.

Revision ID: 001_initial
Revises:
Create Date: 2026-04-23 00:00:00

Reverse-engineered from ``backend/models.py`` as of the first auth-enabled
release. Before this migration existed, tables were created implicitly via
``Base.metadata.create_all`` on app startup; production deployments should
``alembic stamp head`` the first time they upgrade so this revision doesn't
try to recreate existing tables.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "batch",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("experiment_type", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("user", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("n_total", sa.Integer(), nullable=True),
        sa.Column(
            "n_done", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "n_failed", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("start_time", sa.Text(), nullable=True),
        sa.Column("end_time", sa.Text(), nullable=True),
        sa.Column("extra", sa.Text(), nullable=True),
    )

    op.create_table(
        "job",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("dataset", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("start_time", sa.Text(), nullable=True),
        sa.Column("end_time", sa.Text(), nullable=True),
        sa.Column("elapsed_s", sa.Integer(), nullable=True),
        sa.Column("metrics", sa.Text(), nullable=True),
        sa.Column("extra", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", "batch_id"),
    )

    op.create_table(
        "event",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("data", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_event_batch_job", "event", ["batch_id", "job_id"]
    )
    op.create_index("idx_event_timestamp", "event", ["timestamp"])

    op.create_table(
        "resource_snapshot",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.Text(), nullable=False),
        sa.Column("gpu_util_pct", sa.Float(), nullable=True),
        sa.Column("gpu_mem_mb", sa.Float(), nullable=True),
        sa.Column("gpu_mem_total_mb", sa.Float(), nullable=True),
        sa.Column("gpu_temp_c", sa.Float(), nullable=True),
        sa.Column("cpu_util_pct", sa.Float(), nullable=True),
        sa.Column("ram_mb", sa.Float(), nullable=True),
        sa.Column("ram_total_mb", sa.Float(), nullable=True),
        sa.Column("disk_free_mb", sa.Float(), nullable=True),
        sa.Column("extra", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_resource_host_ts", "resource_snapshot", ["host", "timestamp"]
    )


def downgrade() -> None:
    op.drop_index("idx_resource_host_ts", table_name="resource_snapshot")
    op.drop_table("resource_snapshot")
    op.drop_index("idx_event_timestamp", table_name="event")
    op.drop_index("idx_event_batch_job", table_name="event")
    op.drop_table("event")
    op.drop_table("job")
    op.drop_table("batch")
