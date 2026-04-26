"""merge 019 email + 020 disk_total heads

Revision ID: 9342ed1b1f53
Revises: 019_email_system, 020_disk_total
Create Date: 2026-04-25 03:41:22.000708+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9342ed1b1f53'
down_revision: Union[str, Sequence[str], None] = ('019_email_system', '020_disk_total')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
