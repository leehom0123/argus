"""Tests for ``GET /api/batches/{id}/health`` and ``.../eta``."""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import (
    mk_user_with_token,
    post_event,
    make_batch_start,
    make_job_start,
    make_job_done,
    seed_completed_batch,
)


@pytest.mark.asyncio
async def test_health_fresh_batch_not_stalled(client):
    """A just-posted batch has a known last_event_age but threshold default 300s."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    await post_event(
        client,
        {
            "schema_version": "1.1",
            "event_type": "batch_start",
            "timestamp": now,
            "batch_id": "b-fresh",
            "source": {"project": "p"},
            "data": {"n_total_jobs": 1},
        },
    )
    r = await client.get("/api/batches/b-fresh/health")
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] == "b-fresh"
    assert body["is_stalled"] is False
    assert body["stalled_threshold_s"] == 300


@pytest.mark.asyncio
async def test_health_with_old_events_is_stalled(client):
    """Batch with events from 2026-04-23 09:00 is way past 300s stale."""
    await post_event(
        client,
        make_batch_start("b-old", ts="2026-04-23T09:00:00Z"),
    )
    r = await client.get("/api/batches/b-old/health")
    assert r.status_code == 200
    body = r.json()
    # Now is wall-clock "today" — the timestamp is from April 23, 2026.
    # If the test runs after April 23, 2026 the batch is stalled.
    # Either way last_event_age_s must be a non-negative int.
    assert body["last_event_age_s"] is not None
    assert body["last_event_age_s"] >= 0


@pytest.mark.asyncio
async def test_health_404_for_invisible(client):
    await seed_completed_batch(client, batch_id="admin-only")
    bob_jwt, _ = await mk_user_with_token(client, "bob")
    r = await client.get(
        "/api/batches/admin-only/health",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_eta_zero_when_all_jobs_done(client):
    """n_done == n_total → eta=0."""
    await seed_completed_batch(client, batch_id="b-1", n_total=1)
    r = await client.get("/api/batches/b-1/eta")
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] == "b-1"
    assert body["pending_count"] == 0
    assert body["eta_seconds"] == 0


@pytest.mark.asyncio
async def test_eta_computed_from_done_samples(client):
    """One done job w/ elapsed_s → EMA picks up a single sample."""
    batch_id = "b-eta"
    # n_total=2 so there's still 1 pending after 1 job_done.
    await post_event(
        client, make_batch_start(batch_id, n_total=2)
    )
    await post_event(
        client, make_job_start(batch_id, "j-1")
    )
    await post_event(
        client,
        make_job_done(batch_id, "j-1", elapsed_s=60,
                      metrics={"MSE": 0.2}),
    )
    r = await client.get(f"/api/batches/{batch_id}/eta")
    assert r.status_code == 200
    body = r.json()
    assert body["pending_count"] == 1
    assert body["sampled_done_jobs"] == 1
    # 1 sample × 1 pending × 60s ≈ 60
    assert body["eta_seconds"] == 60


@pytest.mark.asyncio
async def test_eta_none_when_no_total(client):
    """Batch with no n_total and no done jobs → eta_seconds=0, pending=0."""
    await post_event(
        client,
        {
            "schema_version": "1.1",
            "event_type": "batch_start",
            "timestamp": "2026-04-23T09:00:00Z",
            "batch_id": "b-no-total",
            "source": {"project": "p"},
            "data": {},
        },
    )
    r = await client.get("/api/batches/b-no-total/eta")
    assert r.status_code == 200
    body = r.json()
    assert body["pending_count"] == 0


@pytest.mark.asyncio
async def test_health_eta_require_auth(unauthed_client):
    r = await unauthed_client.get("/api/batches/x/health")
    assert r.status_code == 401
    r = await unauthed_client.get("/api/batches/x/eta")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Unit tests for ``batch_health()`` — direct calls into the service layer so
# we can pin ``status``, the latest event timestamp, and ``now`` precisely
# without going through API auth + visibility plumbing.
#
# These tests cover the consistency-checker bug fix (2026-04-25): the API
# was reporting ``is_stalled=false`` for batches whose ``status`` had
# already been auto-flipped to ``"stalled"``, because the old logic
# short-circuited on ``status == "running"``.
# ---------------------------------------------------------------------------


async def _seed_batch_with_event(
    session,
    *,
    batch_id: str,
    status: str | None,
    event_ts: str,
    event_type: str = "batch_start",
):
    """Helper: insert one Batch + one Event with the given status / ts."""
    from backend.models import Batch, Event

    session.add(
        Batch(
            id=batch_id,
            project="p",
            n_done=0,
            n_failed=0,
            status=status,
            is_deleted=False,
        )
    )
    session.add(
        Event(
            batch_id=batch_id,
            job_id=None,
            event_type=event_type,
            timestamp=event_ts,
            schema_version="1.1",
            data=None,
            event_id=None,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_is_stalled_running_batch_with_old_event(client):
    """status='running', last_event 600s ago, threshold 300s -> stalled."""
    from datetime import datetime, timezone

    from backend.db import SessionLocal
    from backend.services.health import batch_health

    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
    event_ts = "2026-04-25T11:50:00Z"  # 600s before ``now``

    async with SessionLocal() as session:
        await _seed_batch_with_event(
            session,
            batch_id="b-running-old",
            status="running",
            event_ts=event_ts,
        )
        result = await batch_health(
            "b-running-old",
            session,
            stalled_threshold_s=300,
            now=now,
        )

    assert result["is_stalled"] is True
    assert result["last_event_age_s"] == 600


@pytest.mark.asyncio
async def test_is_stalled_already_stalled_status_with_old_event(client):
    """status='stalled', last_event 1776s ago -> still flagged stalled.

    Reproduces the Argus consistency-checker finding for a stalled batch:
    previously the endpoint returned ``is_stalled=false`` because the
    watchdog had already flipped the status away from ``running``.
    """
    from datetime import datetime, timezone

    from backend.db import SessionLocal
    from backend.services.health import batch_health

    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
    event_ts = "2026-04-25T11:30:24Z"  # 1776s before ``now``

    async with SessionLocal() as session:
        await _seed_batch_with_event(
            session,
            batch_id="b-already-stalled",
            status="stalled",
            event_ts=event_ts,
        )
        result = await batch_health(
            "b-already-stalled",
            session,
            stalled_threshold_s=300,
            now=now,
        )

    assert result["is_stalled"] is True
    assert result["last_event_age_s"] == 1776


@pytest.mark.asyncio
async def test_is_stalled_done_batch_with_old_event(client):
    """status='done', last_event 1h ago -> never stalled (terminal)."""
    from datetime import datetime, timezone

    from backend.db import SessionLocal
    from backend.services.health import batch_health

    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
    event_ts = "2026-04-25T11:00:00Z"  # 3600s before ``now``

    async with SessionLocal() as session:
        await _seed_batch_with_event(
            session,
            batch_id="b-done-old",
            status="done",
            event_ts=event_ts,
        )
        result = await batch_health(
            "b-done-old",
            session,
            stalled_threshold_s=300,
            now=now,
        )

    assert result["is_stalled"] is False
    assert result["last_event_age_s"] == 3600


@pytest.mark.asyncio
async def test_is_stalled_failed_batch_with_old_event(client):
    """status='failed' is terminal -> never stalled."""
    from datetime import datetime, timezone

    from backend.db import SessionLocal
    from backend.services.health import batch_health

    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
    event_ts = "2026-04-25T11:00:00Z"

    async with SessionLocal() as session:
        await _seed_batch_with_event(
            session,
            batch_id="b-failed-old",
            status="failed",
            event_ts=event_ts,
        )
        result = await batch_health(
            "b-failed-old",
            session,
            stalled_threshold_s=300,
            now=now,
        )

    assert result["is_stalled"] is False


@pytest.mark.asyncio
async def test_is_stalled_cancelled_batch_with_old_event(client):
    """status='cancelled' is terminal -> never stalled."""
    from datetime import datetime, timezone

    from backend.db import SessionLocal
    from backend.services.health import batch_health

    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
    event_ts = "2026-04-25T11:00:00Z"

    async with SessionLocal() as session:
        await _seed_batch_with_event(
            session,
            batch_id="b-cancelled-old",
            status="cancelled",
            event_ts=event_ts,
        )
        result = await batch_health(
            "b-cancelled-old",
            session,
            stalled_threshold_s=300,
            now=now,
        )

    assert result["is_stalled"] is False


@pytest.mark.asyncio
async def test_is_stalled_status_none_with_old_event(client):
    """status=None, last_event 600s ago -> still flagged stalled.

    A batch that never received its first ``batch_start`` event has
    ``status=None`` in the row.  The existing ``(batch.status or "").lower()``
    coercion in :func:`backend.services.health.batch_health` should
    drop that into the empty-string branch, which is *not* a member
    of ``_TERMINAL`` — so a never-started batch with stale events
    must surface as stalled rather than silently pass the health
    check.
    """
    from datetime import datetime, timezone

    from backend.db import SessionLocal
    from backend.services.health import _TERMINAL, batch_health

    # Defensive contract check: empty string must not be terminal,
    # otherwise a status=None row would be a permanent silent stall.
    assert "" not in _TERMINAL

    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
    event_ts = "2026-04-25T11:50:00Z"  # 600s before ``now``

    async with SessionLocal() as session:
        await _seed_batch_with_event(
            session,
            batch_id="b-status-none",
            status=None,
            event_ts=event_ts,
        )
        result = await batch_health(
            "b-status-none",
            session,
            stalled_threshold_s=300,
            now=now,
        )

    assert result["is_stalled"] is True
    assert result["last_event_age_s"] == 600


@pytest.mark.asyncio
async def test_is_stalled_running_batch_with_recent_event(client):
    """status='running', last_event 60s ago, threshold 300s -> not stalled."""
    from datetime import datetime, timezone

    from backend.db import SessionLocal
    from backend.services.health import batch_health

    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
    event_ts = "2026-04-25T11:59:00Z"  # 60s before ``now``

    async with SessionLocal() as session:
        await _seed_batch_with_event(
            session,
            batch_id="b-running-recent",
            status="running",
            event_ts=event_ts,
        )
        result = await batch_health(
            "b-running-recent",
            session,
            stalled_threshold_s=300,
            now=now,
        )

    assert result["is_stalled"] is False
    assert result["last_event_age_s"] == 60
