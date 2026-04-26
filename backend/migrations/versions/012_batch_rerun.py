"""Add Batch.source_batch_id for the "Rerun with overrides" feature.

Revision ID: 012_batch_rerun
Revises: 011_notifications
Create Date: 2026-04-24 00:00:12

Adds a nullable ``source_batch_id`` column to :class:`Batch`. When a user
clicks "Rerun with overrides" from the dashboard, the backend creates a
brand new ``Batch`` row carrying the original id in this column so the
UI (and the polling reporter-side launcher) can trace the lineage. The
column is indexed to make "all rerun children of batch X" lookups cheap.

The FK is nullable and SET NULL on delete so purging an old batch does
not cascade-delete its rerun descendants — the lineage is lost, which
is the correct tradeoff (the descendants stand on their own).

Downgrade drops the index and column.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "012_batch_rerun"
down_revision: Union[str, Sequence[str], None] = "011_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite cannot ALTER TABLE ADD FOREIGN KEY after the fact for every
    # configuration, so we add the column as a plain nullable Text; the
    # logical FK is documented and enforced in the ORM / service layer.
    # (Mirrors the pattern used by migration 008 for ``batch_id`` on
    # resource_snapshot.)
    with op.batch_alter_table("batch") as batch_op:
        batch_op.add_column(
            sa.Column("source_batch_id", sa.Text(), nullable=True)
        )

    op.create_index(
        "idx_batch_source_batch_id",
        "batch",
        ["source_batch_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_batch_source_batch_id", table_name="batch")
    with op.batch_alter_table("batch") as batch_op:
        batch_op.drop_column("source_batch_id")
