"""Add active_sessions table for JWT session tracking + /settings/sessions panel.

Revision ID: 015_active_sessions
Revises: fbf34c3f9980
Create Date: 2026-04-24 10:00:00

The ``active_sessions`` table mirrors the subset of JWT metadata the
Settings > Sessions panel needs to render "here is every device with an
unexpired token, and you can revoke any of them". Tokens are still signed
JWTs — this table is additive and does not replace the blacklist; the
request auth path consults both so a revoke takes effect within one
request cycle.

Columns:
  jti           — PK; URL-safe random string already minted by
                  ``create_access_token`` so we reuse it as the session id.
  user_id       — FK to user.id; indexed for the "my sessions" listing.
  issued_at     — ISO 8601 timestamp when the JWT was issued.
  expires_at    — ISO 8601 timestamp when the JWT naturally expires.
  user_agent    — HTTP User-Agent header captured at login, nullable.
  ip            — Client IP captured at login, nullable (GDPR-adjacent:
                  we keep it for 'identify this device' context; a future
                  retention job can purge old rows).
  last_seen_at  — ISO 8601 timestamp, bumped by ``get_current_user`` every
                  time this JWT is presented. Gives the UI an "active N
                  minutes ago" chip.
  revoked_at    — Nullable ISO 8601. NULL = still active; set when the user
                  hits ``POST /api/auth/sessions/{jti}/revoke``. We keep
                  the row so the UI can show a history of revocations if
                  we ever add that tab; the blacklist does the actual
                  enforcement.

Indexes:
  idx_active_sessions_user      (user_id)  — list caller's sessions
  idx_active_sessions_expires   (expires_at) — future retention sweep

Downgrade drops the table and both indexes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "015_active_sessions"
down_revision: Union[str, Sequence[str], None] = "fbf34c3f9980"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "active_sessions",
        sa.Column("jti", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issued_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_active_sessions_user", "active_sessions", ["user_id"]
    )
    op.create_index(
        "idx_active_sessions_expires", "active_sessions", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_active_sessions_expires", table_name="active_sessions")
    op.drop_index("idx_active_sessions_user", table_name="active_sessions")
    op.drop_table("active_sessions")
