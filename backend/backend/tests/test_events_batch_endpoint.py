"""POST /api/events/batch — bulk ingest endpoint."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select


def _ev(event_id: str, i: int) -> dict:
    return {
        "event_id": event_id,
        "schema_version": "1.1",
        "event_type": "job_epoch",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b-bulk",
        "job_id": f"job-{i}",
        "source": {"project": "p"},
        "data": {"epoch": 1, "train_loss": 0.5},
    }


@pytest.mark.asyncio
async def test_batch_ingest_50_events(client):
    import backend.db as db_mod
    from backend.models import Event

    events = [_ev(str(uuid.uuid4()), i) for i in range(50)]
    r = await client.post("/api/events/batch", json={"events": events})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 50
    assert body["rejected"] == 0
    assert len(body["results"]) == 50
    assert all(item["status"] == "accepted" for item in body["results"])
    assert all(item["db_id"] is not None for item in body["results"])

    async with db_mod.SessionLocal() as session:
        count = await session.scalar(
            select(func.count()).select_from(Event)
        )
        assert count == 50


@pytest.mark.asyncio
async def test_batch_ingest_partial_dedup(client):
    """Re-sending a batch with overlapping event_ids dedupes on the overlap."""
    events = [_ev(str(uuid.uuid4()), i) for i in range(5)]
    r1 = await client.post("/api/events/batch", json={"events": events})
    assert r1.status_code == 200
    assert r1.json()["accepted"] == 5

    # Resend exactly the same batch.
    r2 = await client.post("/api/events/batch", json={"events": events})
    assert r2.status_code == 200
    body = r2.json()
    assert body["accepted"] == 5  # still accepted (as dedup), not rejected
    statuses = [it["status"] for it in body["results"]]
    assert all(s == "deduplicated" for s in statuses)


@pytest.mark.asyncio
async def test_batch_ingest_over_500_rejected(client):
    """Pydantic's max_length=500 on BatchEventsIn short-circuits huge payloads."""
    events = [_ev(str(uuid.uuid4()), i) for i in range(501)]
    r = await client.post("/api/events/batch", json={"events": events})
    # Pydantic validation → 422 (field-level), not a 200 with 501 rejected.
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_batch_ingest_bad_event_in_middle(client):
    """A single malformed event is rejected; good events still commit."""
    good1 = _ev(str(uuid.uuid4()), 0)
    bad = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_epoch",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b-bulk",
        "job_id": "job-bad",
        "source": {"project": "p"},
        # Missing required field 'epoch' → 422 inside _ingest_one.
        "data": {"train_loss": 0.5},
    }
    good2 = _ev(str(uuid.uuid4()), 2)

    r = await client.post(
        "/api/events/batch", json={"events": [good1, bad, good2]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 2
    assert body["rejected"] == 1
    assert body["results"][0]["status"] == "accepted"
    assert body["results"][1]["status"] == "rejected"
    assert body["results"][2]["status"] == "accepted"
