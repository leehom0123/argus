"""Add notification table for in-app watchdog alerts.

Revision ID: 011_notifications
Revises: 010_demo_project
Create Date: 2026-04-24 00:00:11

Creates the ``notification`` table used by the watchdog rule engine to
surface in-app alerts without requiring SMTP or Feishu. Each row records
one rule firing for one user; ``read_at`` is NULL until the user
acknowledges it via the API bell.

Columns:
  id          — autoincrement PK
  user_id     — FK → user(id) CASCADE DELETE
  batch_id    — nullable text FK (no CASCADE — batch delete leaves orphans
                 that can be GC'd by the retention sweeper)
  rule_id     — short string identifying the watchdog rule
  severity    — 'info' | 'warn' | 'error'
  title       — short alert heading
  body        — longer description
  created_at  — ISO 8601 text timestamp
  read_at     — nullable ISO 8601; set by ACK endpoints

Indexes:
  idx_notification_user_created  (user_id, created_at) — list endpoint
  idx_notification_batch         (batch_id) — fast lookup on batch delete

Downgrade drops the table and both indexes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "011_notifications"
down_revision: Union[str, Sequence[str], None] = "010_demo_project"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("read_at", sa.Text(), nullable=True),
    )

    op.create_index(
        "idx_notification_user_created",
        "notification",
        ["user_id", "created_at"],
    )
    op.create_index(
        "idx_notification_batch",
        "notification",
        ["batch_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_notification_batch", table_name="notification")
    op.drop_index("idx_notification_user_created", table_name="notification")
    op.drop_table("notification")
