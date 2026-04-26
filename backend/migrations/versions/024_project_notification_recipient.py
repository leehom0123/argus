"""Per-project email recipient list (multi-recipient notifications).

Revision ID: 024_project_notification_recipient
Revises: 023_batch_email_subscription
Create Date: 2026-04-25 23:00:00

Adds the ``project_notification_recipient`` table that lets a project
owner register an arbitrary mailbox (Argus user OR external address)
to receive email notifications for a project.  Each row carries:

* ``id``                  — surrogate primary key
* ``project``             — free-form project name (no FK; project is
                            inferred from ``batch.project``)
* ``email``               — recipient mailbox; not bound to ``user.email``
* ``event_kinds``         — JSON-encoded list of event_type strings
* ``enabled``             — bool gate; the unsubscribe link flips this
                            to ``false`` without deleting the row
* ``added_by_user_id``    — FK to the user who registered the row;
                            cascade on user delete so an audit trail
                            never outlives its actor
* ``unsubscribe_token``   — URL-safe random secret used by the public
                            ``GET /api/unsubscribe/recipient/{token}``
                            endpoint
* ``created_at``/``updated_at`` — ISO 8601 strings, populated by the
                                  API layer

Indexes
-------
* ``ix_pnr_project`` on ``project`` for the dispatcher's
  per-event-batch lookup.
* ``uq_pnr_project_email`` UNIQUE composite on ``(project, email)``
  so re-adding the same address returns 409 instead of duplicating.
* ``unsubscribe_token`` is declared unique inline on the column for
  the token-lookup path.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "024_project_notification_recipient"
down_revision: Union[str, Sequence[str], None] = "023_batch_email_subscription"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_notification_recipient",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
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
        sa.Column(
            "added_by_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "unsubscribe_token",
            sa.Text(),
            nullable=False,
            unique=True,
        ),
        sa.Column("created_at", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_pnr_project",
        "project_notification_recipient",
        ["project"],
    )
    op.create_index(
        "uq_pnr_project_email",
        "project_notification_recipient",
        ["project", "email"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pnr_project_email",
        table_name="project_notification_recipient",
    )
    op.drop_index(
        "ix_pnr_project",
        table_name="project_notification_recipient",
    )
    op.drop_table("project_notification_recipient")
