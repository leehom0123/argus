"""Tests for the val-loss divergence detector (Team A / roadmap #12).

The detector lives in ``backend.notifications.watchdog._check_batch_divergence``
and runs from ``watchdog_loop_once``. It mutates ``batch.status`` to
``'divergent'`` and inserts a ``batch_diverged`` event whenever val_loss
either doubles over 3 consecutive epochs or becomes NaN/Inf.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.db import SessionLocal
from backend.models import Batch, Event, Job
from backend.notifications.watchdog import (
    _check_batch_divergence,
    watchdog_loop_once,
)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed_batch(
    db,
    batch_id: str = "b1",
    status: str = "running",
    losses: list[float] | None = None,
    job_id: str = "j1",
) -> Batch:
    now = datetime.now(timezone.utc)
    batch = Batch(
        id=batch_id,
        project="p1",
        status=status,
        start_time=_iso(now),
    )
    db.add(batch)
    db.add(Job(id=job_id, batch_id=batch_id, status="running"))
    if losses:
        for i, vl in enumerate(losses):
            db.add(
                Event(
                    batch_id=batch_id,
                    job_id=job_id,
                    event_type="job_epoch",
                    timestamp=_iso(now - timedelta(minutes=len(losses) - i)),
                    schema_version="1.1",
                    data=json.dumps({"epoch": i, "val_loss": vl}),
                )
            )
    await db.commit()
    return batch


@pytest.mark.asyncio
async def test_divergence_monotonic_doubles(client):
    """3 strictly-rising epochs with ≥2× ratio → flagged divergent."""
    async with SessionLocal() as db:
        await _seed_batch(db, losses=[0.10, 0.14, 0.30])
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b1"))
        ).scalar_one()
        events = list(
            (
                await db.execute(select(Event).where(Event.batch_id == "b1"))
            ).scalars()
        )
        events.sort(key=lambda e: e.timestamp, reverse=True)

        fired = await _check_batch_divergence(db, batch, events)
        await db.commit()

    assert fired is True
    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b1"))
        ).scalar_one()
        assert b.status == "divergent"
        evs = list(
            (
                await db.execute(
                    select(Event).where(Event.event_type == "batch_diverged")
                )
            ).scalars()
        )
        assert len(evs) == 1
        payload = json.loads(evs[0].data)
        assert payload["reason"] == "ratio"
        assert payload["ratio"] is not None
        assert payload["ratio"] >= 2.0


@pytest.mark.asyncio
async def test_divergence_nan_short_circuits(client):
    """A single NaN val_loss is enough — no monotonic-growth check required."""
    async with SessionLocal() as db:
        await _seed_batch(
            db, batch_id="b-nan", losses=[0.1, 0.1, float("nan")]
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-nan"))
        ).scalar_one()
        events = list(
            (
                await db.execute(select(Event).where(Event.batch_id == "b-nan"))
            ).scalars()
        )
        events.sort(key=lambda e: e.timestamp, reverse=True)

        fired = await _check_batch_divergence(db, batch, events)
        await db.commit()

    assert fired is True
    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-nan"))
        ).scalar_one()
        assert b.status == "divergent"
        evs = list(
            (
                await db.execute(
                    select(Event).where(Event.event_type == "batch_diverged")
                )
            ).scalars()
        )
        assert len(evs) == 1
        assert json.loads(evs[0].data)["reason"] == "nan_or_inf"


@pytest.mark.asyncio
async def test_divergence_noisy_but_stable_does_not_fire(client):
    """val_loss oscillates but never doubles → no flag."""
    async with SessionLocal() as db:
        await _seed_batch(
            db, batch_id="b-stable", losses=[0.20, 0.21, 0.19, 0.22]
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-stable"))
        ).scalar_one()
        events = list(
            (
                await db.execute(
                    select(Event).where(Event.batch_id == "b-stable")
                )
            ).scalars()
        )
        events.sort(key=lambda e: e.timestamp, reverse=True)

        fired = await _check_batch_divergence(db, batch, events)
        await db.commit()

    assert fired is False
    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-stable"))
        ).scalar_one()
        assert b.status == "running"


@pytest.mark.asyncio
async def test_divergence_already_flagged_skipped(client):
    """A batch already at status=divergent short-circuits (no duplicate event)."""
    async with SessionLocal() as db:
        await _seed_batch(
            db,
            batch_id="b-already",
            status="divergent",
            losses=[0.1, 0.2, 0.4, 0.8],
        )
        batch = (
            await db.execute(select(Batch).where(Batch.id == "b-already"))
        ).scalar_one()
        events = list(
            (
                await db.execute(
                    select(Event).where(Event.batch_id == "b-already")
                )
            ).scalars()
        )
        events.sort(key=lambda e: e.timestamp, reverse=True)

        fired = await _check_batch_divergence(db, batch, events)

    assert fired is False


@pytest.mark.asyncio
async def test_watchdog_loop_once_integrates_divergence(client):
    """Full scan: running batch with bad losses → divergent + event emitted."""
    async with SessionLocal() as db:
        await _seed_batch(db, batch_id="b-full", losses=[0.1, 0.3, 0.9])
        await watchdog_loop_once(db)

    async with SessionLocal() as db:
        b = (
            await db.execute(select(Batch).where(Batch.id == "b-full"))
        ).scalar_one()
        assert b.status == "divergent"
        evs = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == "b-full")
                    .where(Event.event_type == "batch_diverged")
                )
            ).scalars()
        )
        assert len(evs) == 1
