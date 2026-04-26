"""JWT dual-key rotation bookkeeping (v0.2 #109).

Revision ID: 031_jwt_dual_key
Revises: 030_token_user_binding
Create Date: 2026-04-26 12:00:00

Stores the active JWT signing secret + the previous one (for the 24h
grace window after a rotation) inside ``system_config`` under the
``jwt`` group. The model + table already exist from #107
(``025_system_config``); this migration only seeds canonical rows
**when missing** so a fresh install boots without manual SQL and an
existing deployment is untouched (operator may have already rotated).

Three keys live under group ``jwt``:

* ``current_secret``  — Fernet-encrypted, the secret used for every
  fresh ``jwt.encode`` call.  ``encrypted=True``.
* ``previous_secret`` — Fernet-encrypted, the previous secret kept
  alive for 24h so already-issued tokens still verify.
  ``encrypted=True``.  Empty string == "no previous secret".
* ``rotated_at``      — UTC ISO timestamp of the last rotation.  Plain
  text (NOT encrypted) so the admin UI can render the countdown
  without decrypting first.

When ``current_secret`` is missing the verifier falls back to
``ARGUS_JWT_SECRET`` (the original env var path), so this migration
is a no-op on boot until an admin clicks "Rotate" for the first time.
We deliberately do NOT pre-populate ``current_secret`` from the env
var — copying a long-lived env secret into the DB ciphertext would
freeze it across future env-rotation events. The empty row is the
"DB has no opinion, use the env" sentinel.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "031_jwt_dual_key"
down_revision: Union[str, Sequence[str], None] = "030_token_user_binding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Insert the three ``jwt.*`` rows iff none exist yet.

    Uses raw SQL (``INSERT ... SELECT ... WHERE NOT EXISTS``) so the
    same statement works on SQLite (the dev/test target) and Postgres
    (production). ``value_json`` for empty secrets is the JSON literal
    ``""`` — a quoted empty string — so the runtime-config decoder
    treats it as "absent" and falls back through the env / default
    chain. ``rotated_at`` ships as ``null`` — the rotate endpoint sets
    it on the first rotation.
    """
    bind = op.get_bind()
    rows = [
        # (key, value_json, encrypted, description)
        (
            "current_secret",
            '""',
            True,
            "Active JWT signing secret. Rotated by POST /api/admin/security/jwt/rotate.",
        ),
        (
            "previous_secret",
            '""',
            True,
            "Previous JWT signing secret. Honoured during the 24h grace window after rotation.",
        ),
        (
            "rotated_at",
            "null",
            False,
            "UTC ISO timestamp of the last rotation. NULL means never rotated.",
        ),
    ]
    for key, value_json, encrypted, description in rows:
        bind.execute(
            sa.text(
                """
                INSERT INTO system_config
                    ("group", key, value_json, encrypted, description, updated_at)
                SELECT CAST(:g AS VARCHAR), CAST(:k AS VARCHAR), CAST(:v AS TEXT),
                       :e, CAST(:d AS TEXT), NULL
                WHERE NOT EXISTS (
                    SELECT 1 FROM system_config
                     WHERE "group" = CAST(:g AS VARCHAR)
                       AND key = CAST(:k AS VARCHAR)
                )
                """
            ),
            {"g": "jwt", "k": key, "v": value_json, "e": encrypted, "d": description},
        )


def downgrade() -> None:
    """Remove the three seeded rows. Operator-rotated secrets are also
    deleted — downgrade is a "reset to env-only" action, which is
    the only sensible inverse of "make rotation possible"."""
    bind = op.get_bind()
    bind.execute(
        sa.text('DELETE FROM system_config WHERE "group" = :g'),
        {"g": "jwt"},
    )
