"""Soft-delete support for jobs, projects, and hosts.

Revision ID: 021_soft_delete_entities
Revises: 9342ed1b1f53
Create Date: 2026-04-25 12:00:00

The ``Batch`` table has carried ``is_deleted`` since migration 003 and
every visibility query already filters on it. This migration extends
the same "soft delete, never lose audit data" pattern to the three
remaining entity surfaces in the UI:

* ``job.is_deleted`` (BOOLEAN, default 0) — flipped by the per-job
  delete endpoint (owner or admin). Existing list / detail queries
  filter on it so a deleted job disappears immediately. Original row
  stays so /api/events history and join-back analytics keep working.
* ``project_meta.is_deleted`` (BOOLEAN, default 0) — projects aren't
  a first-class table, so we mark them deleted on the existing
  ``project_meta`` row. The delete endpoint also cascades by setting
  ``Batch.is_deleted=True`` for every batch under the project (which
  the existing visibility filter handles).
* ``host_meta`` (NEW table) — hosts also aren't a first-class row;
  they're derived from ``ResourceSnapshot.host``. We add a tiny meta
  table so an admin can hide a retired host from the UI without
  dropping its snapshot history.

All defaults are ``False`` so existing rows behave unchanged after the
upgrade. Downgrade drops the columns and the new table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "021_soft_delete_entities"
down_revision: Union[str, Sequence[str], None] = "9342ed1b1f53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- Job.is_deleted -------------------------------------------------
    with op.batch_alter_table("job") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_deleted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # ---- ProjectMeta.is_deleted ----------------------------------------
    with op.batch_alter_table("project_meta") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_deleted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # ---- host_meta -----------------------------------------------------
    op.create_table(
        "host_meta",
        sa.Column("host", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("deleted_at", sa.Text(), nullable=True),
        sa.Column(
            "deleted_by_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("hidden_at", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_host_meta_is_deleted",
        "host_meta",
        ["is_deleted"],
    )


def downgrade() -> None:
    op.drop_index("idx_host_meta_is_deleted", table_name="host_meta")
    op.drop_table("host_meta")

    with op.batch_alter_table("project_meta") as batch_op:
        batch_op.drop_column("is_deleted")

    with op.batch_alter_table("job") as batch_op:
        batch_op.drop_column("is_deleted")
