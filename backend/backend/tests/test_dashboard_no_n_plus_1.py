"""Regression test for the project-card ETA N+1 query.

Before the fix, :meth:`DashboardService._project_cards` called
``_project_eta`` once per visible project, and each call ran its own
``SELECT FROM job WHERE batch_id IN (running batches of THIS project)``
— so K projects meant K extra queries. The fix hoists the elapsed-jobs
lookup into a single ``SELECT FROM job WHERE batch_id IN (all running
batches across all projects)`` and partitions in Python before calling
``ema_eta`` per project.

This test instruments the SQLAlchemy engine with a ``before_cursor_execute``
listener and counts how many ``SELECT … FROM job …`` statements that
look like the ETA elapsed-sample query fire during a single
``GET /api/dashboard`` call. Asserts the count is **at most one**
regardless of the number of projects, so the next refactor can't
silently re-introduce the loop.
"""
from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import event

import backend.db as db_mod
from backend.tests.conftest import _now_iso
from backend.utils.response_cache import default_cache as _response_cache


# Regex matches the elapsed-sample shape: ``SELECT … job.elapsed_s …
# FROM job WHERE … job.elapsed_s IS NOT NULL …``. We don't pin the
# ``status='done'`` literal because SQLAlchemy parameterises it (the
# raw SQL contains ``= ?`` not ``= 'done'``). Both the pre-fix
# per-project query and the post-fix single hoisted query match — the
# assertion is on the COUNT, not the shape.
_ETA_QUERY_RE = re.compile(
    r"select\s+.*\bjob\.elapsed_s\b.*\bfrom\s+job\b"
    r".*\bjob\.elapsed_s\s+is\s+not\s+null",
    re.IGNORECASE | re.DOTALL,
)


def _install_query_counter(engine) -> dict:
    """Attach a ``before_cursor_execute`` listener returning a counter dict.

    Returns a dict with ``"queries": list[str]`` populated as queries
    fire. Caller is responsible for removing the listener (we do that
    via ``finally`` in the test body).
    """
    state: dict = {"queries": []}

    sync_engine = engine.sync_engine

    def _on_exec(_conn, _cursor, statement, _params, _context, _executemany):
        state["queries"].append(statement)

    event.listen(sync_engine, "before_cursor_execute", _on_exec)
    state["_remove"] = lambda: event.remove(
        sync_engine, "before_cursor_execute", _on_exec
    )
    return state


async def _seed_running_batch(client, batch_id: str, project: str) -> None:
    """Post a batch_start under ``project`` so it counts as a running batch.

    No job rows; the ETA path needs the batch to be running but doesn't
    require any done samples (it short-circuits to ``None`` when there
    are no done jobs, which is fine for query-count assertions).

    The timestamp is anchored to wall-clock UTC so the seeded batch row
    keeps showing up in the dashboard's 24-hour ``jobs_done_24h`` /
    ``batches_this_week`` rollups regardless of the calendar date the
    suite runs on. A hardcoded ``2026-04-25T12:00:00Z`` would silently
    age out of those windows once the suite ran on a later week,
    masking any future regression in the per-project query path the
    test is pinning.
    """
    ev = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": _now_iso(),
        "batch_id": batch_id,
        "source": {"project": project, "user": "tester"},
        "data": {"n_total_jobs": 4},
    }
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_project_eta_query_does_not_scale_with_project_count(client):
    """Dashboard call fires ≤1 ETA-sample query regardless of project count.

    Seeds one running batch per project across K=6 projects, then calls
    ``GET /api/dashboard`` and counts how many SELECTs match the ETA
    elapsed-sample shape. Pre-fix this would be 6 (one per project);
    post-fix it must be ≤1.
    """
    K = 6
    for i in range(K):
        await _seed_running_batch(client, f"b-eta-{i}", f"proj-eta-{i}")

    # Drop any cache state so the dashboard loader actually runs.
    _response_cache.clear()

    state = _install_query_counter(db_mod.engine)
    try:
        r = await client.get("/api/dashboard")
        assert r.status_code == 200, r.text
        body = r.json()
        # All K projects are visible.
        names = {p["project"] for p in body["projects"]}
        for i in range(K):
            assert f"proj-eta-{i}" in names, names
    finally:
        state["_remove"]()

    matching = [q for q in state["queries"] if _ETA_QUERY_RE.search(q)]
    # The pre-fix code fired one per project (≥K). The post-fix code
    # fires exactly one IN-query for all running batches across all
    # projects, so the count must collapse to ≤1.
    assert len(matching) <= 1, (
        f"Expected ≤1 ETA elapsed-sample query for {K} projects, "
        f"got {len(matching)}:\n" + "\n".join(matching)
    )


@pytest.mark.asyncio
async def test_project_eta_query_count_constant_across_sizes(client):
    """Count holds at K=2 and K=8 — proves the fix is constant, not just small.

    A naïve "≤1" assertion could pass for K=1 by accident; this test
    seeds two distinct K values and asserts the ETA-sample query count
    is the same in both cases.
    """
    async def _count_for(num_projects: int) -> int:
        # Reset DB state via the client fixture's purge — we can't
        # re-fixture inside one test, so instead seed fresh batch ids
        # under fresh project names so the query partition changes.
        for i in range(num_projects):
            await _seed_running_batch(
                client,
                f"b-eta-sz-{num_projects}-{i}",
                f"proj-eta-sz-{num_projects}-{i}",
            )
        _response_cache.clear()
        st = _install_query_counter(db_mod.engine)
        try:
            r = await client.get("/api/dashboard")
            assert r.status_code == 200
        finally:
            st["_remove"]()
        return sum(
            1 for q in st["queries"] if _ETA_QUERY_RE.search(q)
        )

    n_small = await _count_for(2)
    n_large = await _count_for(8)
    # Both must be ≤1, AND equal to each other (constant).
    assert n_small == n_large, (
        f"ETA query count grows with project count: "
        f"{n_small} (K=2) vs {n_large} (K=8)"
    )
    assert n_large <= 1, (
        f"Expected ≤1 ETA elapsed-sample query for K=8, got {n_large}"
    )
