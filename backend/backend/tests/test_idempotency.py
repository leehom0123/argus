"""Idempotency: duplicate terminal events must be no-ops, not errors."""
from __future__ import annotations

import uuid

import pytest


def _eid() -> str:
    """Fresh UUID event_id — each logical POST under v1.1 needs one."""
    return str(uuid.uuid4())


async def _post(client, ev):
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_duplicate_job_done_is_ok(client):
    """Two distinct job_done events (different event_id + timestamp) still
    trigger last-write-wins on the job row."""
    src = {"project": "p"}
    await _post(client, {
        "event_id": _eid(),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b1",
        "source": src,
        "data": {"n_total_jobs": 1},
    })
    await _post(client, {
        "event_id": _eid(),
        "schema_version": "1.1",
        "event_type": "job_start",
        "timestamp": "2026-04-23T09:01:00Z",
        "batch_id": "b1",
        "job_id": "j1",
        "source": src,
        "data": {"model": "m", "dataset": "d"},
    })
    done_ev_v1 = {
        "event_id": _eid(),
        "schema_version": "1.1",
        "event_type": "job_done",
        "timestamp": "2026-04-23T09:02:00Z",
        "batch_id": "b1",
        "job_id": "j1",
        "source": src,
        "data": {"status": "DONE", "elapsed_s": 60,
                 "metrics": {"MSE": 0.5}},
    }
    done_ev_v2 = dict(done_ev_v1)
    done_ev_v2["event_id"] = _eid()  # distinct id so it isn't deduped
    done_ev_v2["timestamp"] = "2026-04-23T09:02:05Z"
    done_ev_v2["data"] = {"status": "DONE", "elapsed_s": 65,
                          "metrics": {"MSE": 0.4}}

    await _post(client, done_ev_v1)
    await _post(client, done_ev_v2)  # second time: last-write-wins

    r = await client.get("/api/jobs/b1/j1")
    body = r.json()
    # Last write wins — the second payload's metrics should be the current.
    assert body["metrics"]["MSE"] == 0.4
    assert body["elapsed_s"] == 65

    # Batch counters still reflect exactly one done job.
    r = await client.get("/api/batches/b1")
    assert r.json()["n_done"] == 1


@pytest.mark.asyncio
async def test_duplicate_batch_done_is_ok(client):
    """Same event payload with same event_id is explicitly deduped; same
    payload with a fresh event_id re-applies the last-write-wins update.
    Either way the batch ends up in status ``done``."""
    src = {"project": "p"}
    await _post(client, {
        "event_id": _eid(),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b2",
        "source": src,
        "data": {"n_total_jobs": 2},
    })
    done = {
        "event_id": _eid(),
        "schema_version": "1.1",
        "event_type": "batch_done",
        "timestamp": "2026-04-23T10:00:00Z",
        "batch_id": "b2",
        "source": src,
        "data": {"n_done": 2, "n_failed": 0, "total_elapsed_s": 3600},
    }
    await _post(client, done)           # first insert
    await _post(client, done)           # deduped via event_id
    await _post(client, {**done, "event_id": _eid()})  # fresh id → re-apply
    r = await client.get("/api/batches/b2")
    assert r.json()["status"] == "done"
