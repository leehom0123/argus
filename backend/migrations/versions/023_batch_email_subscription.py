"""Per-batch email subscription overrides.

Revision ID: 023_batch_email_subscription
Revises: e3a032a32a47
Create Date: 2026-04-25 22:00:00

Adds the ``batch_email_subscription`` table that lets a batch owner
override the project-level email defaults for a single batch.  Each
row carries:

* ``user_id`` + ``batch_id``  — composite primary key
* ``event_kinds``             — JSON-encoded list of event_type strings
* ``enabled``                 — bool (kept for forward compat; the
                                public API treats an absent row as
                                "fall back to project default" and
                                exposes a DELETE to clear an override)
* ``created_at`` / ``updated_at`` — ISO 8601 strings, populated by
                                    the API layer

Indexes on ``batch_id`` and ``user_id`` so the dispatcher can do a
single-row lookup keyed by (user, batch) without scanning the table.
Foreign keys cascade-delete the row when either the user or the batch
is hard-deleted, mirroring how :class:`NotificationSubscription` ties
to ``user.id``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "023_batch_email_subscription"
down_revision: Union[str, Sequence[str], None] = "e3a032a32a47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "batch_email_subscription",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "batch_id",
            sa.Text(),
            sa.ForeignKey("batch.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "event_kinds",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_batch_email_sub_batch",
        "batch_email_subscription",
        ["batch_id"],
    )
    op.create_index(
        "idx_batch_email_sub_user",
        "batch_email_subscription",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_batch_email_sub_user", table_name="batch_email_subscription"
    )
    op.drop_index(
        "idx_batch_email_sub_batch", table_name="batch_email_subscription"
    )
    op.drop_table("batch_email_subscription")
