"""Emit + fetch roundtrip test for the env_snapshot event type.

Verifies that:
  1. ``POST /api/events`` with event_type='env_snapshot' is accepted (200)
  2. The payload is stored on the Batch row's ``env_snapshot_json``
  3. ``GET /api/batches/{id}`` returns ``env_snapshot`` as a parsed dict
  4. A second env_snapshot for the same batch does NOT overwrite the first
     (first-write-wins semantics)
"""
from __future__ import annotations

import uuid

import pytest

_BATCH_ID = "env-snap-test-batch"


def _make_event(event_type: str, data: dict, job_id: str | None = None) -> dict:
    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": "2026-04-24T10:00:00Z",
        "batch_id": _BATCH_ID,
        "job_id": job_id,
        "source": {
            "project": "test-project",
            "host": "test-host",
            "user": "tester",
        },
        "data": data,
    }


_SNAP_DATA = {
    "git_sha": "abc1234def5678901234567890abcdef12345678",
    "git_branch": "main",
    "git_dirty": False,
    "python_version": "3.12.3",
    "pip_freeze": ["numpy==1.26.0", "torch==2.3.0"],
    "hydra_config_digest": "sha256hexhere",
    "hydra_config_content": "model:\n  d_model: 128\n",
    "hostname": "test-host",
}

_SNAP_DATA_V2 = {
    **_SNAP_DATA,
    "git_sha": "ffff0000111122223333444455556666777788889999",
    "hostname": "different-host",
}


@pytest.mark.asyncio
async def test_env_snapshot_accepted(client):
    """env_snapshot event is accepted with HTTP 200."""
    ev = _make_event("env_snapshot", _SNAP_DATA)
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True


@pytest.mark.asyncio
async def test_env_snapshot_stored_on_batch(client):
    """After ingesting env_snapshot, GET /api/batches/{id} exposes env_snapshot dict."""
    # Create the batch first via batch_start.
    await client.post(
        "/api/events",
        json=_make_event("batch_start", {"n_total_jobs": 1}),
    )
    # Send env_snapshot.
    await client.post(
        "/api/events",
        json=_make_event("env_snapshot", _SNAP_DATA),
    )

    r = await client.get(f"/api/batches/{_BATCH_ID}")
    assert r.status_code == 200, r.text
    batch = r.json()

    snap = batch.get("env_snapshot")
    assert snap is not None, "env_snapshot field missing from batch response"
    assert snap["git_sha"] == _SNAP_DATA["git_sha"]
    assert snap["python_version"] == "3.12.3"
    assert "numpy==1.26.0" in snap["pip_freeze"]
    assert snap["hydra_config_digest"] == "sha256hexhere"
    assert snap["git_dirty"] is False


@pytest.mark.asyncio
async def test_env_snapshot_first_write_wins(client):
    """A second env_snapshot event for the same batch does not overwrite the first."""
    # batch_start + first snapshot
    await client.post(
        "/api/events",
        json=_make_event("batch_start", {"n_total_jobs": 1}),
    )
    await client.post(
        "/api/events",
        json=_make_event("env_snapshot", _SNAP_DATA),
    )

    # Second snapshot with different git_sha
    ev2 = _make_event("env_snapshot", _SNAP_DATA_V2)
    r2 = await client.post("/api/events", json=ev2)
    assert r2.status_code == 200, r2.text

    r = await client.get(f"/api/batches/{_BATCH_ID}")
    snap = r.json().get("env_snapshot")
    assert snap is not None
    # Should still have the FIRST snapshot's sha.
    assert snap["git_sha"] == _SNAP_DATA["git_sha"], (
        "Second env_snapshot should not overwrite the first"
    )
