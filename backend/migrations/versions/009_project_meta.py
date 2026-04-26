"""Add ``project_meta`` — admin-controlled per-project flags / description.

Revision ID: 009_project_meta
Revises: 008_proc_snapshot
Create Date: 2026-04-24 00:00:03

Projects are not first-class rows elsewhere in the schema: they are just
``Batch.project`` strings aggregated at read time. To let an admin flag
a specific project as a **public demo** (readable by anonymous
visitors), we keep the same key (the project name string) as the
primary key here. No foreign key to ``batch.project`` because it is not
unique — multiple batches share the same project name.

Columns:

* ``project``              (Text, PK) — matches ``batch.project``
* ``is_public``            (Boolean, default False) — admin toggle
* ``public_description``   (Text, nullable, max 500 chars)
* ``published_at``         (Text ISO-8601, nullable)
* ``published_by_user_id`` (Integer FK → user.id, nullable)

Index on ``is_public`` so listing "all public projects" is cheap even
once thousands of projects exist.

Downgrade drops the table + index cleanly.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009_project_meta"
down_revision: Union[str, Sequence[str], None] = "008_proc_snapshot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_meta",
        sa.Column("project", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("public_description", sa.Text(), nullable=True),
        sa.Column("published_at", sa.Text(), nullable=True),
        sa.Column(
            "published_by_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_project_meta_is_public",
        "project_meta",
        ["is_public"],
    )


def downgrade() -> None:
    op.drop_index("idx_project_meta_is_public", table_name="project_meta")
    op.drop_table("project_meta")
