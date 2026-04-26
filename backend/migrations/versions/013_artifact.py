"""Add artifact table for job file uploads (roadmap #8).

Revision ID: 013_artifact
Revises: 012_batch_rerun
Create Date: 2026-04-24 00:00:13

Creates the ``artifact`` table backing the reporter's ``job_artifact()``
upload API + the ``/api/jobs/{id}/artifacts`` and
``/api/artifacts/{id}`` endpoints. Rows carry enough metadata for the
frontend to render type-appropriate previews (image / CSV / JSON /
text) without re-reading the file.

Columns:
  id                    — autoincrement PK
  job_id                — text, indexed (no FK: Job uses composite PK)
  batch_id              — text, indexed (joins to Batch for visibility)
  filename              — original upload filename, display-only
  mime                  — MIME type captured at upload time
  size_bytes            — byte length; used by the 500 MB per-job cap
  label                 — optional group label (e.g. ``visualizations``)
  meta_json             — nullable JSON-encoded dict of extra metadata
  storage_path          — path relative to ``settings.artifact_storage_dir``
  created_at            — ISO 8601 text timestamp
  created_by_user_id    — uploader; nullable for service-account uploads

Indexes:
  idx_artifact_job     (job_id)         — list endpoint
  idx_artifact_batch   (batch_id)       — per-batch rollup / GC

Downgrade drops the table and both indexes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "013_artifact"
down_revision: Union[str, Sequence[str], None] = "012_batch_rerun"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "artifact",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("idx_artifact_job", "artifact", ["job_id"])
    op.create_index("idx_artifact_batch", "artifact", ["batch_id"])


def downgrade() -> None:
    op.drop_index("idx_artifact_batch", table_name="artifact")
    op.drop_index("idx_artifact_job", table_name="artifact")
    op.drop_table("artifact")
