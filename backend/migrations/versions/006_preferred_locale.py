"""Add preferred_locale column to users table.

Revision ID: 006_preferred_locale
Revises: 005_stars_pins_batch_meta
Create Date: 2026-04-24 00:00:00

Stores the user's preferred UI/email locale so communications use their
language rather than the Accept-Language header value at request time.
Default is 'en-US' for backward compatibility with existing rows.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006_preferred_locale"
down_revision: Union[str, Sequence[str], None] = "005_stars_pins_batch_meta"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("user") as user_op:
        user_op.add_column(
            sa.Column(
                "preferred_locale",
                sa.String(16),
                nullable=True,
                server_default="en-US",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user") as user_op:
        user_op.drop_column("preferred_locale")
