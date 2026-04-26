"""Team Email: SMTP config + templates + subscriptions + unsubscribe + dead letter.

Revision ID: 019_email_system
Revises: 018_perf_indexes
Create Date: 2026-04-25 00:00:01

Creates five tables that together back the outbound-email subsystem:

* ``smtp_config``            — single-row admin SMTP settings (id=1 PK)
* ``email_template``         — per (event_type, locale) subject + body
* ``notification_subscription`` — per-user opt-in per (project, event_type)
* ``email_dead_letter``      — failed sends (BE-2 writes, BE-1 defines)
* ``email_unsubscribe_token`` — one-shot 32-char secret for unsubscribe links

All booleans use ``sa.false()`` / ``sa.true()`` so the migration is
portable between SQLite and Postgres.  Text columns use ``sa.Text()`` to
match the rest of the schema (ISO-8601 timestamps, arbitrary identifier
lengths).  Downgrade drops the five tables and their indexes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "019_email_system"
down_revision: Union[str, Sequence[str], None] = "018_perf_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- smtp_config (single row, id=1) --------------------------------
    op.create_table(
        "smtp_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("smtp_host", sa.Text(), nullable=True),
        sa.Column(
            "smtp_port", sa.Integer(), nullable=False, server_default="587"
        ),
        sa.Column("smtp_username", sa.Text(), nullable=True),
        sa.Column("smtp_password_encrypted", sa.Text(), nullable=True),
        sa.Column("smtp_from_address", sa.Text(), nullable=True),
        sa.Column("smtp_from_name", sa.Text(), nullable=True),
        sa.Column(
            "use_tls",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "use_ssl",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("updated_at", sa.Text(), nullable=True),
        sa.Column(
            "updated_by_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # --- email_template ------------------------------------------------
    op.create_table(
        "email_template",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column(
            "is_system",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("updated_at", sa.Text(), nullable=True),
        sa.Column(
            "updated_by_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_email_template_event_locale",
        "email_template",
        ["event_type", "locale"],
        unique=True,
    )

    # --- notification_subscription ------------------------------------
    op.create_table(
        "notification_subscription",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("updated_at", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_notif_sub_user_project_event",
        "notification_subscription",
        ["user_id", "project", "event_type"],
        unique=True,
    )
    op.create_index(
        "idx_notif_sub_user",
        "notification_subscription",
        ["user_id"],
    )

    # --- email_dead_letter --------------------------------------------
    op.create_table(
        "email_dead_letter",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("to_address", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "idx_email_dead_letter_created",
        "email_dead_letter",
        ["created_at"],
    )

    # --- email_unsubscribe_token --------------------------------------
    op.create_table(
        "email_unsubscribe_token",
        sa.Column("token", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_email_unsub_user",
        "email_unsubscribe_token",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_email_unsub_user", table_name="email_unsubscribe_token"
    )
    op.drop_table("email_unsubscribe_token")

    op.drop_index(
        "idx_email_dead_letter_created", table_name="email_dead_letter"
    )
    op.drop_table("email_dead_letter")

    op.drop_index(
        "idx_notif_sub_user", table_name="notification_subscription"
    )
    op.drop_index(
        "idx_notif_sub_user_project_event",
        table_name="notification_subscription",
    )
    op.drop_table("notification_subscription")

    op.drop_index(
        "idx_email_template_event_locale", table_name="email_template"
    )
    op.drop_table("email_template")

    op.drop_table("smtp_config")
