"""Team A guardrails: Job.is_idle_flagged + User.known_ips_json.

Revision ID: 016_guardrails
Revises: fbf34c3f9980
Create Date: 2026-04-24 12:00:00

Adds two columns supporting the Team A "Guardrails" roadmap work:

* ``job.is_idle_flagged`` (BOOLEAN, default 0) — set by the watchdog
  when a job's mean GPU utilisation stays below 5% for longer than
  ``ARGUS_IDLE_JOB_THRESHOLD_MIN`` minutes. Advisory only; nothing
  auto-kills the job.
* ``user.known_ips_json`` (TEXT, nullable) — JSON-encoded list of
  ``{ip, ua_hash, last_seen}`` entries, used by the anomalous-login
  detector in ``/api/auth/login``. Entries older than 30 days are
  pruned on write, so the column stays small.

Downgrade drops both columns.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "016_guardrails"
down_revision: Union[str, Sequence[str], None] = "fbf34c3f9980"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("job") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_idle_flagged",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(
            sa.Column("known_ips_json", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("known_ips_json")
    with op.batch_alter_table("job") as batch_op:
        batch_op.drop_column("is_idle_flagged")
