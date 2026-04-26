"""Backfill orphan ``Batch.owner_id`` from token-authored events (#127).

Revision ID: 030_token_user_binding
Revises: 028_user_notification_prefs
Create Date: 2026-04-26 00:00:00

Bug context: batches stubbed by ``/api/events*`` had ``owner_id=NULL``
because the
ingest path resolved owner via the ``ApiToken.user`` relationship, and
the relationship was not always present in the request greenlet. The
runtime fix (this PR's events.py change) reads ``token.user_id`` from
``request.state`` directly, so newly-created batches will always be
stamped. This migration repairs the legacy rows.

Repair strategy:
  1. For every ``batch`` with ``owner_id IS NULL``, fall back to the
     first admin user (``user.is_admin`` truthy, lowest ``id``). We have
     no direct batch->token link, so an admin owner keeps the row
     visible to admin dashboards and reassignable via the existing
     admin tooling.
  2. If no admin user exists (fresh installs / edge case), skip with a
     WARNING — the runtime fix prevents new orphans from forming so the
     orphan list is bounded.

Chains after ``028_user_notification_prefs`` (the latest dev tip at
write time). Migration 029 may be claimed by ``feat/v0.2-jwt-rotate``;
PM will resolve final ordering at merge if a clash surfaces.
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "030_token_user_binding"
down_revision: Union[str, Sequence[str], None] = "028_user_notification_prefs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()

    # Pick the first admin user as the backfill target. ``ORDER BY id``
    # stably maps to the bootstrap admin. SQLite stores BOOL as 0/1 but
    # accepts comparison to literal TRUE; Postgres rejects bool=integer
    # — use the SQL standard truth-test which works on both backends.
    admin_row = bind.execute(
        sa.text(
            'SELECT id FROM "user" '
            "WHERE is_admin IS TRUE "
            "ORDER BY id ASC LIMIT 1"
        )
    ).fetchone()

    if admin_row is None:
        log.warning(
            "030_token_user_binding: no admin user found; skipping orphan "
            "Batch.owner_id backfill (orphans will remain NULL until an "
            "admin manually reassigns them)."
        )
        return

    admin_id = int(admin_row[0])

    # Count first so the log message is informative even when the UPDATE
    # touches zero rows on a clean install.
    count_row = bind.execute(
        sa.text("SELECT COUNT(*) FROM batch WHERE owner_id IS NULL")
    ).fetchone()
    orphan_count = int(count_row[0]) if count_row is not None else 0

    if orphan_count == 0:
        log.info(
            "030_token_user_binding: no orphan batches; nothing to backfill."
        )
        return

    bind.execute(
        sa.text(
            "UPDATE batch SET owner_id = :admin_id WHERE owner_id IS NULL"
        ),
        {"admin_id": admin_id},
    )
    log.warning(
        "030_token_user_binding: backfilled %d orphan batch row(s) to "
        "admin user_id=%d. Reassign manually via /api/admin if a different "
        "owner is desired.",
        orphan_count,
        admin_id,
    )


def downgrade() -> None:
    # Backfill is informational; reverting it would re-orphan rows that
    # an admin may have intentionally kept. We deliberately make the
    # downgrade a no-op so a partial rollback never destroys ownership.
    pass
