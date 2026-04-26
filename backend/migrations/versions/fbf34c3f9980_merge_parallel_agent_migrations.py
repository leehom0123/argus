"""merge parallel agent migrations

Revision ID: fbf34c3f9980
Revises: 013_artifact, 014_env_snapshot
Create Date: 2026-04-24 07:10:12.405657+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fbf34c3f9980'
down_revision: Union[str, Sequence[str], None] = ('013_artifact', '014_env_snapshot')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
