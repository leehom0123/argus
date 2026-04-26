"""Tests for the idle-job detector (Team A / roadmap #13).

The detector lives in ``backend.notifications.watchdog._check_idle_jobs``.
It flags running jobs whose associated :class:`ResourceSnapshot` rows
show GPU util < 5% spanning at least
``ARGUS_IDLE_JOB_THRESHOLD_MIN`` minutes.  The job is NOT killed; only
``is_idle_flagged`` flips to True and a ``job_idle_flagged`` event
lands in the event table.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.db import SessionLocal
from backend.models import Batch, Event, Job, ResourceSnapshot
from backend.notifications.watchdog import _check_idle_jobs


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed(
    db,
    *,
    batch_id: str = "b1",
    job_id: str = "j1",
    utils: list[float],
    span_minutes: float = 10,
    job_already_flagged: bool = False,
) -> None:
    """Seed a batch + job + N snapshots evenly spaced across ``span_minutes``.

    The oldest snapshot sits at ``now - (span_minutes - 0.1)`` minutes so it
    comfortably survives the detector's ``now - idle_job_threshold_min``
    cutoff (default 10 min) even with clock drift during the test run.
    Snapshots newer than that run forward to (approximately) ``now`` so the
    realized span equals ``span_minutes - 0.2`` min, which is > 0.9×10min.
    """
    now = datetime.now(timezone.utc)
    db.add(Batch(id=batch_id, project="p", status="running", host="gpu-1"))
    db.add(
        Job(
            id=job_id,
            batch_id=batch_id,
            status="running",
            is_idle_flagged=job_already_flagged,
        )
    )
    if utils:
        # Space samples so first sits at ``-span + 0.1`` and last at ``-0.1``.
        effective_span = span_minutes - 0.2
        step = effective_span / max(1, len(utils) - 1)
        for i, u in enumerate(utils):
            offset_min = (span_minutes - 0.1) - step * i
            ts = now - timedelta(minutes=offset_min)
            db.add(
                ResourceSnapshot(
                    host="gpu-1",
                    batch_id=batch_id,
                    timestamp=_iso(ts),
                    gpu_util_pct=u,
                )
            )
    await db.commit()


@pytest.mark.asyncio
async def test_idle_flagged_when_all_samples_low(client):
    """All 5 snapshots < 5% across 11 minutes → flagged."""
    async with SessionLocal() as db:
        await _seed(
            db,
            utils=[0.5, 1.0, 2.0, 0.0, 1.5],
            span_minutes=10,
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b1"))
        ).scalar_one()
        flipped = await _check_idle_jobs(db, batch)
        await db.commit()

    assert len(flipped) == 1
    async with SessionLocal() as db:
        job = (
            await db.execute(select(Job).where(Job.id == "j1"))
        ).scalar_one()
        assert job.is_idle_flagged is True
        evs = list(
            (
                await db.execute(
                    select(Event).where(Event.event_type == "job_idle_flagged")
                )
            ).scalars()
        )
        assert len(evs) == 1
        payload = json.loads(evs[0].data)
        assert payload["job_id"] == "j1"
        assert payload["minutes"] == 10


@pytest.mark.asyncio
async def test_not_flagged_when_any_sample_busy(client):
    """One sample ≥ 5% → detector keeps quiet."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-busy",
            job_id="j-busy",
            utils=[0.5, 1.0, 50.0, 0.0, 1.5],
            span_minutes=10,
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-busy"))
        ).scalar_one()
        flipped = await _check_idle_jobs(db, batch)
        await db.commit()

    assert flipped == []
    async with SessionLocal() as db:
        job = (
            await db.execute(select(Job).where(Job.id == "j-busy"))
        ).scalar_one()
        assert job.is_idle_flagged is False


@pytest.mark.asyncio
async def test_not_flagged_when_window_too_short(client):
    """Samples span only 2 minutes → need ≥10 min → skip."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-short",
            job_id="j-short",
            utils=[0.1, 0.2, 0.3],
            span_minutes=2,
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-short"))
        ).scalar_one()
        flipped = await _check_idle_jobs(db, batch)
        await db.commit()

    assert flipped == []


@pytest.mark.asyncio
async def test_already_flagged_job_is_skipped(client):
    """Flag is sticky — we don't emit duplicate events on repeated scans."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-rep",
            job_id="j-rep",
            utils=[0.1, 0.1, 0.1],
            span_minutes=10,
            job_already_flagged=True,
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-rep"))
        ).scalar_one()
        flipped = await _check_idle_jobs(db, batch)

    assert flipped == []


@pytest.mark.asyncio
async def test_idle_flag_does_not_kill_job(client):
    """Contract: the detector only flags — ``job.status`` stays 'running'."""
    async with SessionLocal() as db:
        await _seed(
            db,
            batch_id="b-live",
            job_id="j-live",
            utils=[0.1, 0.2, 0.3],
            span_minutes=10,
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-live"))
        ).scalar_one()
        await _check_idle_jobs(db, batch)
        await db.commit()

    async with SessionLocal() as db:
        job = (
            await db.execute(select(Job).where(Job.id == "j-live"))
        ).scalar_one()
        assert job.is_idle_flagged is True
        assert job.status == "running"
