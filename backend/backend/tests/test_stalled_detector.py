"""Tests for the stalled-batch detector (heartbeat / ARGUS_STALL_TIMEOUT_MIN).

The detector lives in
``backend.notifications.watchdog._check_stalled_batches`` and runs from
``watchdog_loop_once``. It flips a batch whose last Event (or
ResourceSnapshot) is older than ``ARGUS_STALL_TIMEOUT_MIN`` to
``status='stalled'`` and emits a ``batch_stalled`` event.

Terminal statuses (``done``, ``failed``, ``divergent``, ``stopped``) are
immune. Already-stalled batches short-circuit so the event is only
emitted once per incident.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.config import get_settings
from backend.db import SessionLocal
from backend.models import Batch, Event, ResourceSnapshot
from backend.notifications.watchdog import (
    _check_stalled_batches,
    watchdog_loop_once,
)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed(
    db,
    *,
    batch_id: str,
    status: str,
    last_event_age_min: float | None,
    last_snap_age_min: float | None = None,
) -> None:
    """Seed one batch with one Event (and optionally one ResourceSnapshot).

    ``last_event_age_min=None`` means "no events at all" — the detector
    then falls back to ``batch.start_time`` (also set to ``age_min`` so
    the test can express a zombie-with-no-events case).
    """
    now = datetime.now(timezone.utc)
    # Pick the oldest signal so start_time reflects real age even when
    # the batch has no events yet.
    candidates = [a for a in (last_event_age_min, last_snap_age_min) if a is not None]
    start_age = max(candidates) if candidates else 30.0
    start_iso = _iso(now - timedelta(minutes=start_age))

    db.add(
        Batch(
            id=batch_id,
            project="p1",
            status=status,
            start_time=start_iso,
        )
    )
    if last_event_age_min is not None:
        db.add(
            Event(
                batch_id=batch_id,
                job_id=None,
                event_type="job_epoch",
                timestamp=_iso(now - timedelta(minutes=last_event_age_min)),
                schema_version="1.1",
                data=json.dumps({"epoch": 1}),
            )
        )
    if last_snap_age_min is not None:
        db.add(
            ResourceSnapshot(
                host="gpu-1",
                batch_id=batch_id,
                timestamp=_iso(now - timedelta(minutes=last_snap_age_min)),
                gpu_util_pct=10.0,
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_recent_activity_not_flagged(client):
    """Last event 5 min ago (< 15 min default) → no flag."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-recent",
            status="running",
            last_event_age_min=5,
        )
        flipped = await _check_stalled_batches(db, get_settings())
        await db.commit()

    assert flipped == []
    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-recent"))
        ).scalar_one()
        assert b.status == "running"
        stalled_evs = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == "b-recent")
                    .where(Event.event_type == "batch_stalled")
                )
            ).scalars()
        )
        assert stalled_evs == []


@pytest.mark.asyncio
async def test_stale_batch_flagged(client):
    """Last event 20 min ago → flagged stalled + event emitted."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-stale",
            status="running",
            last_event_age_min=20,
        )
        flipped = await _check_stalled_batches(db, get_settings())
        await db.commit()

    assert len(flipped) == 1
    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-stale"))
        ).scalar_one()
        assert b.status == "stalled"

        stalled_evs = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == "b-stale")
                    .where(Event.event_type == "batch_stalled")
                )
            ).scalars()
        )
        assert len(stalled_evs) == 1
        payload = json.loads(stalled_evs[0].data)
        assert payload["minutes_since"] >= 19  # ~20 min with rounding
        assert payload["last_event_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status", ["done", "failed", "divergent", "stopped"]
)
async def test_terminal_statuses_immune(client, terminal_status):
    """Terminal statuses should never be flagged, regardless of event age."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id=f"b-{terminal_status}",
            status=terminal_status,
            last_event_age_min=120,  # 2 hours old
        )
        flipped = await _check_stalled_batches(db, get_settings())
        await db.commit()

    assert flipped == []
    async with SessionLocal() as db:
        b = (
            await db.execute(
                select(Batch).where(Batch.id == f"b-{terminal_status}")
            )
        ).scalar_one()
        assert b.status == terminal_status


@pytest.mark.asyncio
async def test_already_stalled_not_duplicated(client):
    """A batch already at status=stalled is skipped — no duplicate event."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-already-stalled",
            status="stalled",
            last_event_age_min=60,
        )
        flipped = await _check_stalled_batches(db, get_settings())
        await db.commit()

    assert flipped == []
    async with SessionLocal() as db:
        stalled_evs = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == "b-already-stalled")
                    .where(Event.event_type == "batch_stalled")
                )
            ).scalars()
        )
        assert stalled_evs == []  # none emitted this pass


@pytest.mark.asyncio
async def test_stopping_status_also_catches(client):
    """``stopping`` is non-terminal — should also flip to stalled if dead."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-stopping",
            status="stopping",
            last_event_age_min=30,
        )
        flipped = await _check_stalled_batches(db, get_settings())
        await db.commit()

    assert len(flipped) == 1
    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-stopping"))
        ).scalar_one()
        assert b.status == "stalled"


@pytest.mark.asyncio
async def test_resource_snapshot_keeps_batch_alive(client):
    """An old event but a *recent* snapshot → batch still alive, not flagged.

    This covers reporters that stream telemetry aggressively but have
    long epoch gaps (e.g. TimesNet on big datasets).
    """
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-snap-heartbeat",
            status="running",
            last_event_age_min=30,
            last_snap_age_min=2,
        )
        flipped = await _check_stalled_batches(db, get_settings())
        await db.commit()

    assert flipped == []
    async with SessionLocal() as db:
        b = (
            await db.execute(
                select(Batch).where(Batch.id == "b-snap-heartbeat")
            )
        ).scalar_one()
        assert b.status == "running"


@pytest.mark.asyncio
async def test_watchdog_loop_integrates_stalled(client):
    """Full scan: stale running batch → flipped + event row lands."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-loop",
            status="running",
            last_event_age_min=25,
        )
        await watchdog_loop_once(db)

    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-loop"))
        ).scalar_one()
        assert b.status == "stalled"
        evs = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == "b-loop")
                    .where(Event.event_type == "batch_stalled")
                )
            ).scalars()
        )
        assert len(evs) == 1
