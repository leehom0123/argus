"""Tests for roadmap #21 — FLOPS / throughput hover-card extras on JobOut.

Extends ``GET /api/jobs/{batch_id}/{job_id}`` (and the batch-scoped
``/api/batches/{id}/jobs``) with three flat fields derived from the
job's ``metrics`` JSON:

  * ``avg_batch_time_ms`` — ``Avg_Batch_Time`` (sec) × 1000
  * ``gpu_memory_peak_mb`` — ``GPU_Memory`` (MB, passthrough)
  * ``n_params`` — ``n_params`` / ``Params`` / ``model_params``

All three are optional; None when absent or unparseable.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed_job_with_metrics(
    session, batch_id: str, job_id: str, owner_id: int,
    metrics: dict | None,
) -> None:
    from backend.models import Batch, Job

    existing = await session.get(Batch, batch_id)
    if existing is None:
        session.add(Batch(
            id=batch_id,
            project="p",
            owner_id=owner_id,
            status="done",
            start_time=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
            end_time=_iso(datetime.now(timezone.utc)),
            n_done=1,
        ))
    session.add(Job(
        id=job_id,
        batch_id=batch_id,
        model="transformer",
        dataset="etth1",
        status="done",
        start_time=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
        end_time=_iso(datetime.now(timezone.utc)),
        elapsed_s=3600,
        metrics=json.dumps(metrics) if metrics is not None else None,
    ))
    await session.commit()


async def _tester_id(session) -> int:
    from backend.models import User
    tester = (
        await session.execute(select(User).where(User.username == "tester"))
    ).scalar_one()
    return tester.id


@pytest.mark.asyncio
async def test_job_detail_populates_extras(client):
    """All three fields surface when present in metrics JSON."""
    import backend.db as db_mod

    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_job_with_metrics(
            session, "b-extras-1", "j-1", owner_id,
            {
                "MSE": 0.25,
                "Avg_Batch_Time": 0.164,        # seconds → 164 ms
                "GPU_Memory": 6600.0,
                "n_params": 12_345_678,
            },
        )

    r = await client.get("/api/jobs/b-extras-1/j-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["avg_batch_time_ms"] == pytest.approx(164.0, rel=1e-3)
    assert body["gpu_memory_peak_mb"] == pytest.approx(6600.0, rel=1e-3)
    assert body["n_params"] == 12_345_678
    # Raw metrics dict still round-trips intact
    assert body["metrics"]["MSE"] == 0.25


@pytest.mark.asyncio
async def test_job_detail_missing_extras_are_none(client):
    """When metrics lacks the extras keys, the fields come back as None."""
    import backend.db as db_mod

    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_job_with_metrics(
            session, "b-extras-2", "j-2", owner_id,
            {"MSE": 0.3},  # no Avg_Batch_Time / GPU_Memory / n_params
        )

    r = await client.get("/api/jobs/b-extras-2/j-2")
    assert r.status_code == 200
    body = r.json()
    assert body["avg_batch_time_ms"] is None
    assert body["gpu_memory_peak_mb"] is None
    assert body["n_params"] is None


@pytest.mark.asyncio
async def test_job_detail_null_metrics_is_safe(client):
    """Jobs with no metrics at all still serialise cleanly."""
    import backend.db as db_mod

    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_job_with_metrics(
            session, "b-extras-3", "j-3", owner_id, None,
        )

    r = await client.get("/api/jobs/b-extras-3/j-3")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] is None
    assert body["avg_batch_time_ms"] is None
    assert body["gpu_memory_peak_mb"] is None
    assert body["n_params"] is None


@pytest.mark.asyncio
async def test_job_detail_params_alias_keys(client):
    """``Params`` and ``model_params`` aliases work for n_params."""
    import backend.db as db_mod

    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_job_with_metrics(
            session, "b-extras-4", "j-alias", owner_id,
            {"Params": 42_000},
        )

    r = await client.get("/api/jobs/b-extras-4/j-alias")
    assert r.status_code == 200
    assert r.json()["n_params"] == 42_000


@pytest.mark.asyncio
async def test_batch_jobs_list_also_has_extras(client):
    """The batch-scoped job list surfaces the same extras."""
    import backend.db as db_mod

    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_job_with_metrics(
            session, "b-extras-5", "j-list", owner_id,
            {"Avg_Batch_Time": 0.05, "GPU_Memory": 1024.0},
        )

    r = await client.get("/api/batches/b-extras-5/jobs")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["avg_batch_time_ms"] == pytest.approx(50.0, rel=1e-3)
    assert rows[0]["gpu_memory_peak_mb"] == pytest.approx(1024.0, rel=1e-3)
    assert rows[0]["n_params"] is None


@pytest.mark.asyncio
async def test_job_detail_malformed_metric_values_return_none(client):
    """Non-numeric garbage in a metrics field falls back to None."""
    import backend.db as db_mod

    async with db_mod.SessionLocal() as session:
        owner_id = await _tester_id(session)
        await _seed_job_with_metrics(
            session, "b-extras-6", "j-bad", owner_id,
            {"Avg_Batch_Time": "not-a-number", "GPU_Memory": None, "n_params": "x"},
        )

    r = await client.get("/api/jobs/b-extras-6/j-bad")
    assert r.status_code == 200
    body = r.json()
    assert body["avg_batch_time_ms"] is None
    assert body["gpu_memory_peak_mb"] is None
    assert body["n_params"] is None
