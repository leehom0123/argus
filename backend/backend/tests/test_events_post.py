"""Ingest contract tests: every event_type from the JSON Schema examples."""
from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_post_all_event_types_succeeds(client, sample_events):
    """Each event round-trips with 200 and returns a numeric event_id."""
    for ev in sample_events:
        r = await client.post("/api/events", json=ev)
        assert r.status_code == 200, (ev["event_type"], r.text)
        body = r.json()
        assert body["accepted"] is True
        assert isinstance(body["event_id"], int)


@pytest.mark.asyncio
async def test_batch_row_created_on_batch_start(client, sample_events):
    await client.post("/api/events", json=sample_events[0])  # batch_start
    r = await client.get("/api/batches")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == "bench-test-1"
    assert rows[0]["status"] == "running"
    assert rows[0]["n_total"] == 12


@pytest.mark.asyncio
async def test_job_row_after_job_start(client, sample_events):
    for ev in sample_events[:2]:  # batch_start + job_start
        await client.post("/api/events", json=ev)
    r = await client.get("/api/batches/bench-test-1/jobs")
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "etth1_transformer"
    assert jobs[0]["model"] == "transformer"
    assert jobs[0]["status"] == "running"


@pytest.mark.asyncio
async def test_job_done_sets_metrics_and_counters(client, sample_events):
    for ev in sample_events[:5]:  # up to job_done
        await client.post("/api/events", json=ev)
    r = await client.get("/api/jobs/bench-test-1/etth1_transformer")
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "done"
    assert job["metrics"]["MSE"] == 0.25

    r = await client.get("/api/batches/bench-test-1")
    assert r.status_code == 200
    batch = r.json()
    assert batch["n_done"] == 1
    assert batch["n_failed"] == 0


@pytest.mark.asyncio
async def test_job_epochs_timeseries(client, sample_events):
    for ev in sample_events[:5]:
        await client.post("/api/events", json=ev)
    r = await client.get(
        "/api/jobs/bench-test-1/etth1_transformer/epochs"
    )
    assert r.status_code == 200
    pts = r.json()
    assert [p["epoch"] for p in pts] == [1, 2]
    assert pts[0]["train_loss"] == 0.42
    assert pts[1]["val_loss"] == 0.33


@pytest.mark.asyncio
async def test_resource_snapshot_and_host_listing(client, sample_events):
    # send the resource_snapshot event
    resource_ev = next(e for e in sample_events if e["event_type"] == "resource_snapshot")
    await client.post("/api/events", json=resource_ev)

    r = await client.get("/api/resources/hosts")
    assert r.status_code == 200
    assert r.json() == ["localhost"]

    r = await client.get("/api/resources", params={"host": "localhost"})
    assert r.status_code == 200
    snaps = r.json()
    assert len(snaps) == 1
    assert snaps[0]["gpu_util_pct"] == 80


@pytest.mark.asyncio
async def test_batch_done_marks_status_done(client, sample_events):
    for ev in sample_events:
        await client.post("/api/events", json=ev)
    r = await client.get("/api/batches/bench-test-1")
    assert r.status_code == 200
    assert r.json()["status"] == "done"


@pytest.mark.asyncio
async def test_out_of_order_job_epoch_stubs_rows(client):
    """Sending job_epoch without prior batch_start / job_start should work."""
    ev = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_epoch",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "ooo-1",
        "job_id": "j1",
        "source": {"project": "p"},
        "data": {"epoch": 1, "train_loss": 0.5},
    }
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200
    # The batch and job rows should now exist (stubbed).
    r = await client.get("/api/batches/ooo-1")
    assert r.status_code == 200
    r = await client.get("/api/batches/ooo-1/jobs")
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "j1"
