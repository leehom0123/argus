"""Add Batch.env_snapshot_json for per-batch reproducibility metadata.

Revision ID: 014_env_snapshot
Revises: 013_watchdog_rules
Create Date: 2026-04-24 00:00:14

Adds a single TEXT column ``env_snapshot_json`` to the ``batch`` table.
The column stores a JSON-encoded dict with:
  - git_sha / git_branch / git_dirty
  - python_version
  - pip_freeze  (list of "pkg==version" strings)
  - hydra_config_digest / hydra_config_content (up to ~60 KB inline)
  - hostname

One snapshot per batch, emitted on ``train_begin`` by the Reporter
callback.  TEXT is sufficient — even a full pip freeze + Hydra config stays
well under SQLite's 1 GB row limit, and there is no need for a blob store.
A NULL value means the reporter client did not send an ``env_snapshot``
event (e.g. older client version).

Downgrade removes the column (SQLite: recreate table without it).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_env_snapshot"
down_revision: Union[str, Sequence[str], None] = "012_batch_rerun"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("batch") as batch_op:
        batch_op.add_column(
            sa.Column("env_snapshot_json", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("batch") as batch_op:
        batch_op.drop_column("env_snapshot_json")
