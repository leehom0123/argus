"""Per-user notification preference defaults (#108).

Revision ID: 027_user_notification_prefs
Revises: 026_merge_024_025
Create Date: 2026-04-26 00:00:00

Adds the ``user.notification_prefs_json`` column used by the
``GET / PUT /api/me/notification_prefs`` endpoints. The column is a
nullable TEXT holding a JSON-encoded mapping of the five UI-facing
pref keys (``notify_batch_done``, ``notify_batch_failed``,
``notify_job_failed``, ``notify_diverged``, ``notify_job_idle``) to
booleans. NULL means "user has not customised" and the API surfaces
the canonical defaults.

Chains after ``026_merge_024_025`` (the canonical merge of the two
parallel heads ``024_project_notification_recipient`` and
``025_system_config`` already on dev). The branch originally tried to
double as the merge migration itself, but dev shipped the merge first
so this revision now sits linearly on top.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "028_user_notification_prefs"
down_revision: Union[str, Sequence[str], None] = "027_executor_agent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite-friendly: ALTER TABLE ADD COLUMN with no default + NULL is
    # supported on every SQLAlchemy backend we care about. We do NOT set
    # a server_default — leaving the column NULL means "use canonical
    # defaults" so we don't have to ship a JSON literal that has to stay
    # in sync with the API layer.
    op.add_column(
        "user",
        sa.Column("notification_prefs_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # ``user`` is touched by many FKs; SQLite + alembic batch mode handles
    # this transparently. Other backends drop the column directly.
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("notification_prefs_json")
