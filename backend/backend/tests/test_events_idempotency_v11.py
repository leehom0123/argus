"""event_id-based idempotency on POST /api/events (schema v1.1).

The existing ``test_idempotency.py`` covers *state* idempotency (duplicate
job_done updates job row correctly). These tests cover *wire* idempotency:
the same event_id resubmitted returns the original db_id and does not
insert a second row.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select


def _base_event(event_id: str, batch_id: str = "b-idem") -> dict:
    return {
        "event_id": event_id,
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
    }


@pytest.mark.asyncio
async def test_duplicate_event_id_returns_same_db_id(client):
    """Second POST of the same event_id returns 200 + deduplicated=true."""
    import backend.db as db_mod
    from backend.models import Event

    eid = str(uuid.uuid4())
    ev = _base_event(eid)

    r1 = await client.post("/api/events", json=ev)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["deduplicated"] is False
    db_id = body1["event_id"]

    r2 = await client.post("/api/events", json=ev)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["deduplicated"] is True
    assert body2["event_id"] == db_id

    # Exactly one row in the event table for this event_id.
    async with db_mod.SessionLocal() as session:
        count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.event_id == eid)
        )
        assert count == 1


@pytest.mark.asyncio
async def test_different_event_ids_insert_multiple_rows(client):
    import backend.db as db_mod
    from backend.models import Event

    ev1 = _base_event(str(uuid.uuid4()), batch_id="b-multi")
    ev2 = _base_event(str(uuid.uuid4()), batch_id="b-multi")
    ev2["event_type"] = "batch_done"
    ev2["timestamp"] = "2026-04-23T10:00:00Z"
    ev2["data"] = {"n_done": 1, "n_failed": 0}

    r1 = await client.post("/api/events", json=ev1)
    r2 = await client.post("/api/events", json=ev2)
    assert r1.status_code == 200
    assert r2.status_code == 200

    async with db_mod.SessionLocal() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.batch_id == "b-multi")
        )
        assert count == 2


@pytest.mark.asyncio
async def test_v11_event_without_event_id_is_422(client):
    """v1.1 clients MUST include event_id; backend rejects missing."""
    bad = {
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b-missing-id",
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
    }
    r = await client.post("/api/events", json=bad)
    assert r.status_code == 422
    assert "event_id" in str(r.json()["detail"]).lower()


@pytest.mark.asyncio
async def test_v10_event_is_rejected_415(client):
    """v1.0 was the transitional wire format; Phase-3 M2 flipped to
    strict enforcement so anything other than v1.1 now returns 415 with
    a machine-readable ``supported`` list."""
    ev = {
        "schema_version": "1.0",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b-v10",
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
    }
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 415, r.text
    body = r.json()
    detail = body["detail"]
    assert detail["supported"] == ["1.1"]
    assert detail["received"] == "1.0"
