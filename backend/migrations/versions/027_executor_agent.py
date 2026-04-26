"""Executor agent_host + agent_command tables (#103 v0.1.5 slice).

Revision ID: 027_executor_agent
Revises: 026_merge_024_025
Create Date: 2026-04-26 09:00:00

Adds two tables that back the Executor service:

* ``agent_host`` — one row per host running ``argus-agent``. Holds the
  hashed agent token (plaintext shown exactly once at registration,
  same pattern as :class:`backend.models.ApiToken`), declared
  capabilities, and last-seen heartbeat timestamp.
* ``agent_command`` — a queue of pending / in-flight commands the
  Executor has dispatched to a given host. Each command carries a
  payload (e.g. ``{command, cwd, env}`` for ``kind='rerun'``) and a
  status flow ``pending → started → failed``.

This revision sits on top of ``026_merge_024_025``, the no-op merge
that collapsed the two parallel branches off
``023_batch_email_subscription``:

* ``024_project_notification_recipient`` (per-project recipients team)
* ``025_system_config``                  (admin runtime-config team)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "027_executor_agent"
down_revision: Union[str, Sequence[str], None] = "026_merge_024_025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_host",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column("agent_token_hash", sa.Text(), nullable=False),
        sa.Column("capabilities_json", sa.Text(), nullable=True),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("registered_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=True),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("user.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_agent_host_token_hash",
        "agent_host",
        ["agent_token_hash"],
        unique=True,
    )
    op.create_index(
        "idx_agent_host_hostname", "agent_host", ["hostname"]
    )

    op.create_table(
        "agent_command",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "host_id",
            sa.Text(),
            sa.ForeignKey("agent_host.id"),
            nullable=False,
        ),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("ack_at", sa.Text(), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_agent_command_host_status",
        "agent_command",
        ["host_id", "status"],
    )
    op.create_index(
        "idx_agent_command_batch", "agent_command", ["batch_id"]
    )
    op.create_index(
        "idx_agent_command_batch_kind_status",
        "agent_command",
        ["batch_id", "kind", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_agent_command_batch_kind_status", table_name="agent_command")
    op.drop_index("idx_agent_command_batch", table_name="agent_command")
    op.drop_index("idx_agent_command_host_status", table_name="agent_command")
    op.drop_table("agent_command")
    op.drop_index("idx_agent_host_hostname", table_name="agent_host")
    op.drop_index("idx_agent_host_token_hash", table_name="agent_host")
    op.drop_table("agent_host")
