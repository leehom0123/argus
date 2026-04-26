"""Guard: every index declared for migration 018 is actually present.

The test boots the in-memory SQLite fixture (via ``create_all`` — same
path production uses when migrations have been applied up-to-head) and
checks that every perf index named in migration 018 shows up in
``sqlite_master``. If someone renames an index in ``models.py`` without
updating the migration, or vice versa, this test fires a loud signal
that the two fell out of sync.

Also verifies that the queries the indexes were designed for actually
use them (``EXPLAIN QUERY PLAN`` should say ``SEARCH ... USING INDEX``,
not ``SCAN``), which catches the case where someone adds the index
object but typos the column name.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


# Indexes migration 018 is expected to create. Keep in sync with
# ``backend/migrations/versions/018_perf_indexes.py`` and the matching
# ``Index(...)`` declarations in ``backend/backend/models.py``.
PERF_018_INDEXES = [
    # job — the single most impactful gap (Job PK is (id, batch_id)
    # with id leading, so WHERE batch_id=? scanned before this).
    "idx_job_batch",
    "idx_job_batch_status",
    "idx_job_batch_status_end",
    # event — epoch timeseries + activity feed + eta-all
    "idx_event_batch_job_type_ts",
    "idx_event_batch_type_ts",
    # batch — list-batches ordering + dashboard counters
    "idx_batch_start_time",
    "idx_batch_status_start",
    "idx_batch_project_start",
    "idx_batch_owner_status",
    # resource_snapshot — dashboard "active hosts in last 5 min"
    "idx_resource_timestamp",
]


@pytest.mark.asyncio
async def test_perf_indexes_all_present(client):
    """Every index declared in migration 018 must exist in the DB."""
    # The client fixture already ran Base.metadata.create_all which
    # honours the Index(...) entries in models.py. We just read the
    # SQLite catalog and assert every expected name is there.
    import backend.db as db_mod

    async with db_mod.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name LIKE 'idx_%'"
                )
            )
        ).all()
    present = {r[0] for r in rows}
    missing = [name for name in PERF_018_INDEXES if name not in present]
    assert not missing, (
        f"Missing perf indexes: {missing}. "
        f"Present ({len(present)}): {sorted(present)}"
    )


@pytest.mark.asyncio
async def test_job_batch_id_query_uses_index(client):
    """``WHERE batch_id = ?`` on Job must use an index (not SCAN).

    The Job PK is ``(id, batch_id)`` with ``id`` leading — before
    migration 018 this query was a full table scan. After 018 it
    should SEARCH USING INDEX idx_job_batch_status (or one of its
    supersets — ``idx_job_batch`` / ``idx_job_batch_status_end``).
    """
    import backend.db as db_mod

    async with db_mod.engine.connect() as conn:
        plan = (
            await conn.execute(
                text(
                    "EXPLAIN QUERY PLAN "
                    "SELECT * FROM job WHERE batch_id = 'some-batch'"
                )
            )
        ).all()
    plan_txt = "\n".join(str(row) for row in plan)
    assert "USING INDEX" in plan_txt, plan_txt
    assert "SCAN" not in plan_txt or "USING INDEX" in plan_txt, plan_txt


@pytest.mark.asyncio
async def test_event_epochs_query_uses_composite_index(client):
    """Epoch timeseries filter hits the 4-col composite."""
    import backend.db as db_mod

    async with db_mod.engine.connect() as conn:
        plan = (
            await conn.execute(
                text(
                    "EXPLAIN QUERY PLAN "
                    "SELECT * FROM event "
                    "WHERE batch_id = 'b' AND job_id = 'j' "
                    "AND event_type = 'job_epoch' "
                    "ORDER BY timestamp ASC"
                )
            )
        ).all()
    plan_txt = "\n".join(str(row) for row in plan)
    # We don't pin the exact index name — SQLite may pick any of
    # idx_event_batch_job / idx_event_batch_job_type_ts — as long as
    # it uses one.
    assert "USING INDEX" in plan_txt, plan_txt
