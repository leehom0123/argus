"""Admin-editable runtime configuration table.

Revision ID: 025_system_config
Revises: 023_batch_email_subscription
Create Date: 2026-04-25 12:00:00

Adds the ``system_config`` table that lets admins edit OAuth /
SMTP / retention / feature-flag / demo-project knobs from the
Settings → Admin UI without redeploying.  The helper
:func:`backend.services.runtime_config.get_config` reads this table
first, then falls back to ``ARGUS_*`` env vars, then to per-call
defaults.

The parallel multi-recipient team owns revision ``024``.  We pick
``025`` and chain off the same ``023_batch_email_subscription`` parent
so the two heads can be merged with a plain ``alembic merge`` once
both branches land.

Schema notes
------------
* ``(group, key)`` composite primary key — keeps lookups O(1) and
  forbids accidental collisions across config groups.
* ``value_json`` is TEXT holding a JSON-encoded scalar / list / object,
  matching the existing ``feature_flag.value_json`` convention.
* ``encrypted=True`` rows store Fernet ciphertext in ``value_json``
  (still valid JSON — a quoted string).  See
  :mod:`backend.services.secrets`.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "025_system_config"
down_revision: Union[str, Sequence[str], None] = "023_batch_email_subscription"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_config",
        sa.Column("group", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("key", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column(
            "encrypted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "updated_by",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_system_config_group", "system_config", ["group"]
    )


def downgrade() -> None:
    op.drop_index("idx_system_config_group", table_name="system_config")
    op.drop_table("system_config")
