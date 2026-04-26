"""GitHub OAuth identity columns on the user table.

Revision ID: 007_github_oauth
Revises: 006_preferred_locale
Create Date: 2026-04-24 00:00:01

Adds three nullable columns to ``user``:

* ``github_id``     — stable numeric GitHub user id (stored as TEXT so we
  never lose leading zeros or hit int overflow).
* ``github_login``  — human-readable GitHub username; display only.
* ``auth_provider`` — ``'local'`` | ``'github'``. Default ``'local'``
  for every existing row so the new column is backfilled safely.

Also relaxes ``user.password_hash`` to nullable — OAuth-provisioned
users have no local password. Existing rows keep their hash unchanged.

A UNIQUE index on ``github_id`` enforces one-to-one linkage. SQLite
treats multiple NULLs as distinct, so the single unique index is enough
to permit many un-linked local users while blocking duplicate GitHub
ids. PostgreSQL would enforce the same semantics (NULLs are distinct).

``user.password_hash`` is relaxed to nullable via ``batch_alter_table``
so SQLite's "no ALTER COLUMN" limitation is transparently worked
around (alembic rebuilds the table + copies rows).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "007_github_oauth"
down_revision: Union[str, Sequence[str], None] = "006_preferred_locale"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("user") as user_op:
        # Relax password_hash to nullable (OAuth users have no password).
        user_op.alter_column(
            "password_hash",
            existing_type=sa.Text(),
            nullable=True,
        )
        user_op.add_column(
            sa.Column("github_id", sa.Text(), nullable=True)
        )
        user_op.add_column(
            sa.Column("github_login", sa.Text(), nullable=True)
        )
        user_op.add_column(
            sa.Column(
                "auth_provider",
                sa.Text(),
                nullable=False,
                server_default="local",
            )
        )

    op.create_index(
        "idx_user_github_id",
        "user",
        ["github_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_user_github_id", table_name="user")
    with op.batch_alter_table("user") as user_op:
        user_op.drop_column("auth_provider")
        user_op.drop_column("github_login")
        user_op.drop_column("github_id")
        # Restore NOT NULL on password_hash. This will fail if any OAuth
        # user (password_hash IS NULL) was created while 007 was applied
        # — operators are expected to delete / reset those rows first.
        user_op.alter_column(
            "password_hash",
            existing_type=sa.Text(),
            nullable=False,
        )
