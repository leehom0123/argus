"""merge 024 + 025 — both branched off 023_batch_email_subscription

Revision ID: 026_merge_024_025
Revises: 024_project_notification_recipient, 025_system_config
Create Date: 2026-04-26

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "026_merge_024_025"
down_revision: Union[str, Sequence[str], None] = (
    "024_project_notification_recipient",
    "025_system_config",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op merge — both parents already created their tables independently."""
    pass


def downgrade() -> None:
    """No-op merge — downgrade follows individual parents."""
    pass
