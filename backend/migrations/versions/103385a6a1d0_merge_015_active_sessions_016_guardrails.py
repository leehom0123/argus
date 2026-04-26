"""merge 015 active_sessions + 016 guardrails

Revision ID: 103385a6a1d0
Revises: 015_active_sessions, 016_guardrails
Create Date: 2026-04-24 14:02:45.821066+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '103385a6a1d0'
down_revision: Union[str, Sequence[str], None] = ('015_active_sessions', '016_guardrails')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
