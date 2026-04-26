"""Add demo-project flags: ``project_meta.is_demo`` + ``user.hide_demo``.

Revision ID: 010_demo_project
Revises: 009_project_meta
Create Date: 2026-04-24 00:00:10

Two small additions layered on top of 009_project_meta:

* ``project_meta.is_demo`` — ``True`` for the single seeded
  ``__demo_forecast__`` fixture so anonymous visitors see a polished
  demo on the landing page while real user projects stay private.
  Indexed because the anonymous /public/projects query filters on it.
* ``user.hide_demo`` — per-user preference; when ``True`` the
  authenticated Projects / Dashboard listings omit demo rows entirely.

Coordinates with the A-方案 migration (``009_project_meta``): this
revision only adds columns, it does NOT redefine ``project_meta`` — so
if A-方案 is merged first we still upgrade cleanly. If A-方案 has NOT
landed yet, migration 009 is a precondition (revises=009_project_meta).
The column additions use server_default='0' so existing rows get a
defined value without a backfill step.

Downgrade drops both columns + the index.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "010_demo_project"
down_revision: Union[str, Sequence[str], None] = "009_project_meta"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # project_meta.is_demo — flags the built-in demo project.
    op.add_column(
        "project_meta",
        sa.Column(
            "is_demo",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "idx_project_meta_is_demo",
        "project_meta",
        ["is_demo"],
    )

    # user.hide_demo — per-user "don't show me the demo" toggle.
    op.add_column(
        "user",
        sa.Column(
            "hide_demo",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "hide_demo")
    op.drop_index("idx_project_meta_is_demo", table_name="project_meta")
    op.drop_column("project_meta", "is_demo")
