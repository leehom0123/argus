"""Negative tests for the ingest endpoint validation layer."""
from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_missing_required_field_is_422(client):
    bad = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        # missing timestamp and batch_id
        "source": {"project": "p"},
    }
    r = await client.post("/api/events", json=bad)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_unknown_event_type_is_422(client):
    bad = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "wat_is_this",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b1",
        "source": {"project": "p"},
    }
    r = await client.post("/api/events", json=bad)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_extra_envelope_field_rejected(client):
    bad = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b1",
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
        "rogue": "should fail",
    }
    r = await client.post("/api/events", json=bad)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_wrong_schema_version_is_415(client):
    """Unsupported schema_version is 415 per requirements §6.5.

    Phase-3 post-review M2 tightened this to reject v1.0 as well as any
    other non-``"1.1"`` value. The body advertises the accepted list so
    clients can report "upgrade your reporter to v1.1" without parsing
    the human message.
    """
    bad = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "2.0",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b1",
        "source": {"project": "p"},
        "data": {},
    }
    r = await client.post("/api/events", json=bad)
    assert r.status_code == 415
    body = r.json()
    detail = body["detail"]
    assert detail["supported"] == ["1.1"]
    assert detail["received"] == "2.0"


@pytest.mark.asyncio
async def test_v10_schema_version_is_415(client):
    """v1.0 used to be soft-accepted; under the strict contract it must
    now return 415 with the same ``supported`` advertisement."""
    bad = {
        "schema_version": "1.0",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b1",
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
    }
    r = await client.post("/api/events", json=bad)
    assert r.status_code == 415
    body = r.json()
    assert body["detail"]["received"] == "1.0"
    assert body["detail"]["supported"] == ["1.1"]


@pytest.mark.asyncio
async def test_job_epoch_requires_epoch_field(client):
    bad = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_epoch",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b1",
        "job_id": "j1",
        "source": {"project": "p"},
        "data": {"train_loss": 0.5},  # no epoch
    }
    r = await client.post("/api/events", json=bad)
    assert r.status_code == 422
