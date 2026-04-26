"""Auth tables: user + email_verification.

Revision ID: 002_auth_user_email
Revises: 001_initial
Create Date: 2026-04-23 00:00:01

Creates only the two tables owned by BACKEND-A (auth / user). API token,
share, public_share, audit_log, feature_flag and the ``batch.owner_id``
ALTER are deliberately deferred to later migrations owned by BACKEND-B and
BACKEND-C so that agent workstreams stay modular.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_auth_user_email"
down_revision: Union[str, Sequence[str], None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_login", sa.Text(), nullable=True),
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("locked_until", sa.Text(), nullable=True),
        sa.UniqueConstraint("username", name="uq_user_username"),
        sa.UniqueConstraint("email", name="uq_user_email"),
    )

    op.create_table(
        "email_verification",
        sa.Column("token", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column(
            "consumed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_email_verification_user",
        ),
    )
    op.create_index(
        "idx_email_verification_user",
        "email_verification",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_email_verification_user", table_name="email_verification"
    )
    op.drop_table("email_verification")
    op.drop_table("user")
