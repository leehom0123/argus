"""Personal API tokens, batch ownership, and event idempotency.

Revision ID: 003_tokens_owner_idempotency
Revises: 002_auth_user_email
Create Date: 2026-04-23 00:00:02

This is BACKEND-B's slice of the auth/sharing surface:

* ``api_token`` — SHA-256-hashed personal tokens with scope + display_hint.
* ``batch.owner_id`` + ``batch.is_deleted`` — ties each batch to the user
  whose API token first reported it, and supports soft delete.
* ``event.event_id`` + unique index — client-generated UUID that lets the
  backend deduplicate POST retries (spill replay etc.) without accidentally
  storing the same event twice.

BACKEND-C will layer the share tables on top of this foundation.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_tokens_owner_idempotency"
down_revision: Union[str, Sequence[str], None] = "002_auth_user_email"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # api_token
    # ------------------------------------------------------------------
    op.create_table(
        "api_token",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("display_hint", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_used", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.Column(
            "revoked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_api_token_user",
        ),
        sa.UniqueConstraint("token_hash", name="uq_api_token_hash"),
    )
    op.create_index(
        "idx_api_token_lookup", "api_token", ["token_hash", "revoked"]
    )
    op.create_index("idx_api_token_user", "api_token", ["user_id"])

    # ------------------------------------------------------------------
    # batch.owner_id + batch.is_deleted
    # ------------------------------------------------------------------
    # SQLite can't add a FK in ALTER TABLE without batch mode; use
    # render_as_batch (enabled in env.py) so alembic emits a table rebuild.
    with op.batch_alter_table("batch") as batch_op:
        batch_op.add_column(
            sa.Column(
                "owner_id",
                sa.Integer(),
                sa.ForeignKey("user.id", name="fk_batch_owner"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_deleted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.create_index("idx_batch_owner", "batch", ["owner_id"])

    # ------------------------------------------------------------------
    # event.event_id + partial unique index
    # ------------------------------------------------------------------
    with op.batch_alter_table("event") as batch_op:
        batch_op.add_column(sa.Column("event_id", sa.Text(), nullable=True))

    # Partial index: only non-NULL event_ids are unique. SQLite supports
    # "WHERE" on a CREATE INDEX since 3.8; older MySQL / Postgres syntax
    # diverges here, but MVP is SQLite-only.
    op.execute(
        "CREATE UNIQUE INDEX idx_event_id ON event(event_id) "
        "WHERE event_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_event_id")
    with op.batch_alter_table("event") as batch_op:
        batch_op.drop_column("event_id")

    op.drop_index("idx_batch_owner", table_name="batch")
    with op.batch_alter_table("batch") as batch_op:
        batch_op.drop_column("is_deleted")
        batch_op.drop_column("owner_id")

    op.drop_index("idx_api_token_user", table_name="api_token")
    op.drop_index("idx_api_token_lookup", table_name="api_token")
    op.drop_table("api_token")
