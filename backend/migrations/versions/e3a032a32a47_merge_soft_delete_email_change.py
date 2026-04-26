"""merge soft_delete + email_change

Revision ID: e3a032a32a47
Revises: 021_soft_delete_entities, 022_email_change
Create Date: 2026-04-25 07:37:42.507441+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3a032a32a47'
down_revision: Union[str, Sequence[str], None] = ('021_soft_delete_entities', '022_email_change')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
