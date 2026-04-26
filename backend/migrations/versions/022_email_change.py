"""Email-change flow: add ``email_verification.payload`` column.

Revision ID: 022_email_change
Revises: 9342ed1b1f53
Create Date: 2026-04-25 15:00:00

The new ``POST /api/auth/change-email`` endpoint reuses the existing
``email_verification`` table (already used for register-time verify and
password-reset) with a new discriminator ``kind='email_change'``. To bind
the requested new email to the one-shot token we need a place to store
arbitrary opaque payload bytes alongside the token row. A nullable
``payload`` text column is the smallest possible schema delta:

* New column ``email_verification.payload`` (TEXT, NULL) — for
  ``kind='email_change'`` rows it holds the (already-validated, lower-
  cased) new email. Other ``kind`` values keep it NULL.
* No new index — payload is read on the same row that ``token`` keys,
  i.e. via primary-key lookup.

Downgrade drops the column. SQLite needs ``batch_alter_table`` for column
drops; Postgres handles ``DROP COLUMN`` natively.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "022_email_change"
down_revision: Union[str, Sequence[str], None] = "9342ed1b1f53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("email_verification") as batch_op:
        batch_op.add_column(
            sa.Column("payload", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("email_verification") as batch_op:
        batch_op.drop_column("payload")
