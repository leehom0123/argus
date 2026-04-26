"""Filter tests for the batch listing endpoint."""
from __future__ import annotations

import uuid

import pytest


async def _post(client, ev):
    # Each event needs a unique event_id under v1.1.
    ev = {**ev, "event_id": str(uuid.uuid4())}
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_filter_by_project_and_user(client):
    base = {
        "schema_version": "1.1",
        "event_type": "batch_start",
        "data": {"n_total_jobs": 1},
    }
    await _post(client, {
        **base,
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b-a",
        "source": {"project": "projA", "user": "alice"},
    })
    await _post(client, {
        **base,
        "timestamp": "2026-04-23T10:00:00Z",
        "batch_id": "b-b",
        "source": {"project": "projB", "user": "bob"},
    })

    r = await client.get("/api/batches", params={"project": "projA"})
    assert [b["id"] for b in r.json()] == ["b-a"]

    r = await client.get("/api/batches", params={"user": "bob"})
    assert [b["id"] for b in r.json()] == ["b-b"]

    r = await client.get("/api/batches", params={"status": "running"})
    assert {b["id"] for b in r.json()} == {"b-a", "b-b"}


@pytest.mark.asyncio
async def test_filter_by_since_and_limit(client):
    for i, ts in enumerate([
        "2026-04-23T08:00:00Z",
        "2026-04-23T09:00:00Z",
        "2026-04-23T10:00:00Z",
    ]):
        await _post(client, {
            "schema_version": "1.1",
            "event_type": "batch_start",
            "timestamp": ts,
            "batch_id": f"b-{i}",
            "source": {"project": "p"},
            "data": {"n_total_jobs": 1},
        })
    r = await client.get(
        "/api/batches", params={"since": "2026-04-23T09:00:00Z"}
    )
    ids = [b["id"] for b in r.json()]
    assert set(ids) == {"b-1", "b-2"}

    r = await client.get("/api/batches", params={"limit": 1})
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_get_batch_404(client):
    r = await client.get("/api/batches/nope")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_jobs_404_for_missing_batch(client):
    r = await client.get("/api/batches/nope/jobs")
    assert r.status_code == 404
