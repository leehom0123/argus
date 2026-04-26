"""Stars, pins, and batch name/tag metadata.

Revision ID: 005_stars_pins_batch_meta
Revises: 004_sharing_admin_audit
Create Date: 2026-04-23 00:00:04

BACKEND-E's slice — Dashboard IA phase. Everything additive:

* ``user_star`` — per-user favourites, polymorphic over project+batch.
  Composite PK ``(user_id, target_type, target_id)`` keeps toggles
  idempotent (re-POST = update starred_at but same row).
* ``user_pin`` — per-user compare-pool, capped at 4 rows by the API
  layer (DB doesn't enforce count; the service does).
* ``batch.name`` / ``batch.tag`` — optional display / filter metadata.
  Required for the phase-4 editor path (PATCH /api/batches/{id}) but
  this migration only adds the columns; no write endpoint consumes
  them yet.

No modifications beyond the two additive ``ALTER`` columns on
``batch`` (nullable TEXT, no default) and the two new tables.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005_stars_pins_batch_meta"
down_revision: Union[str, Sequence[str], None] = "004_sharing_admin_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # user_star
    # ------------------------------------------------------------------
    op.create_table(
        "user_star",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("starred_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_user_star_user",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "target_type", "target_id", name="pk_user_star"
        ),
    )
    op.create_index(
        "idx_user_star_target", "user_star", ["target_type", "target_id"]
    )

    # ------------------------------------------------------------------
    # user_pin
    # ------------------------------------------------------------------
    op.create_table(
        "user_pin",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("pinned_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_user_pin_user",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.id"],
            ondelete="CASCADE",
            name="fk_user_pin_batch",
        ),
        sa.PrimaryKeyConstraint("user_id", "batch_id", name="pk_user_pin"),
    )
    op.create_index("idx_user_pin_batch", "user_pin", ["batch_id"])

    # ------------------------------------------------------------------
    # batch.name + batch.tag (phase 4 editor prerequisite)
    # ------------------------------------------------------------------
    with op.batch_alter_table("batch") as batch_op:
        batch_op.add_column(sa.Column("name", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("tag", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("batch") as batch_op:
        batch_op.drop_column("tag")
        batch_op.drop_column("name")

    op.drop_index("idx_user_pin_batch", table_name="user_pin")
    op.drop_table("user_pin")
    op.drop_index("idx_user_star_target", table_name="user_star")
    op.drop_table("user_star")
