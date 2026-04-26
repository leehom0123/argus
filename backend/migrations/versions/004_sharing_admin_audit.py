"""Sharing, admin, audit log, feature flags.

Revision ID: 004_sharing_admin_audit
Revises: 003_tokens_owner_idempotency
Create Date: 2026-04-23 00:00:03

BACKEND-C's slice. Everything here is additive:

* ``batch_share`` — per-batch grants (viewer / editor) from owner to grantee.
* ``project_share`` — blanket grant from owner for a project to a grantee.
* ``public_share`` — tokenised public read-only URL, optional expiry.
* ``audit_log`` — append-only record of auth / share / admin events.
* ``feature_flag`` — admin-toggleable global flags (``registration_open`` etc).

No modifications to existing tables beyond the FKs pointing at ``user.id`` /
``batch.id``. Down-migration drops the five tables in reverse order.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004_sharing_admin_audit"
down_revision: Union[str, Sequence[str], None] = "003_tokens_owner_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # batch_share
    # ------------------------------------------------------------------
    op.create_table(
        "batch_share",
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("grantee_id", sa.Integer(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.id"],
            ondelete="CASCADE",
            name="fk_batch_share_batch",
        ),
        sa.ForeignKeyConstraint(
            ["grantee_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_batch_share_grantee",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["user.id"],
            name="fk_batch_share_creator",
        ),
        sa.PrimaryKeyConstraint(
            "batch_id", "grantee_id", name="pk_batch_share"
        ),
    )
    op.create_index(
        "idx_batch_share_grantee", "batch_share", ["grantee_id"]
    )

    # ------------------------------------------------------------------
    # project_share
    # ------------------------------------------------------------------
    op.create_table(
        "project_share",
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("grantee_id", sa.Integer(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_project_share_owner",
        ),
        sa.ForeignKeyConstraint(
            ["grantee_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_project_share_grantee",
        ),
        sa.PrimaryKeyConstraint(
            "owner_id", "project", "grantee_id", name="pk_project_share"
        ),
    )
    op.create_index(
        "idx_project_share_grantee", "project_share", ["grantee_id"]
    )
    op.create_index(
        "idx_project_share_owner_project",
        "project_share",
        ["owner_id", "project"],
    )

    # ------------------------------------------------------------------
    # public_share
    # ------------------------------------------------------------------
    op.create_table(
        "public_share",
        sa.Column("slug", sa.Text(), primary_key=True),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.Column(
            "view_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_viewed", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.id"],
            ondelete="CASCADE",
            name="fk_public_share_batch",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["user.id"],
            name="fk_public_share_creator",
        ),
    )
    op.create_index(
        "idx_public_share_batch", "public_share", ["batch_id"]
    )

    # ------------------------------------------------------------------
    # audit_log
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.Text(), nullable=False),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_audit_log_user",
        ),
    )
    op.create_index(
        "idx_audit_log_timestamp", "audit_log", ["timestamp"]
    )
    op.create_index(
        "idx_audit_log_user_action",
        "audit_log",
        ["user_id", "action"],
    )

    # ------------------------------------------------------------------
    # feature_flag
    # ------------------------------------------------------------------
    op.create_table(
        "feature_flag",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["user.id"],
            name="fk_feature_flag_updater",
        ),
    )


def downgrade() -> None:
    op.drop_table("feature_flag")
    op.drop_index("idx_audit_log_user_action", table_name="audit_log")
    op.drop_index("idx_audit_log_timestamp", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("idx_public_share_batch", table_name="public_share")
    op.drop_table("public_share")
    op.drop_index(
        "idx_project_share_owner_project", table_name="project_share"
    )
    op.drop_index("idx_project_share_grantee", table_name="project_share")
    op.drop_table("project_share")
    op.drop_index("idx_batch_share_grantee", table_name="batch_share")
    op.drop_table("batch_share")
