"""Tests for POST /batches/{id}/stop and GET /batches/{id}/stop-requested."""
from __future__ import annotations

import uuid

import pytest


def _batch_start_event(batch_id: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-24T10:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "proj"},
        "data": {"n_total_jobs": 2},
    }


async def _mk_user(client, username: str) -> tuple[str, str]:
    """Register + login + mint reporter token. Returns (jwt, api_token)."""
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
    )
    jwt = lr.json()["access_token"]
    tr = await client.post(
        "/api/tokens",
        json={"name": f"{username}-rep", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    return jwt, tr.json()["token"]


@pytest.mark.asyncio
async def test_stop_batch_happy_path(client):
    """Owner can stop a running batch; status flips to 'stopping'."""
    # Create batch owned by the default tester
    r = await client.post("/api/events", json=_batch_start_event("stop-batch-1"))
    assert r.status_code == 200

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # POST /stop using the owner's JWT
    r = await client.post(
        "/api/batches/stop-batch-1/stop",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "stopping"
    assert body["batch_id"] == "stop-batch-1"

    # Batch status must now be 'stopping'
    r = await client.get(
        "/api/batches/stop-batch-1",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.json()["status"] == "stopping"

    # stop-requested endpoint must report requested=True
    r = await client.get(
        "/api/batches/stop-batch-1/stop-requested",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["requested"] is True
    assert data["requested_by"] == "tester"
    assert data["requested_at"] is not None


@pytest.mark.asyncio
async def test_stop_batch_non_owner_403(client):
    """Non-owner receives 403 when attempting to stop another user's batch."""
    # Create batch owned by tester
    r = await client.post("/api/events", json=_batch_start_event("stop-batch-2"))
    assert r.status_code == 200

    # Register alice (non-owner)
    alice_jwt, _ = await _mk_user(client, "alice")

    r = await client.post(
        "/api/batches/stop-batch-2/stop",
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_stop_batch_idempotent(client):
    """Calling /stop twice on the same batch returns 200 both times."""
    r = await client.post("/api/events", json=_batch_start_event("stop-batch-3"))
    assert r.status_code == 200

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r1 = await client.post(
        "/api/batches/stop-batch-3/stop",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r1.status_code == 200

    # Second call — idempotent
    r2 = await client.post(
        "/api/batches/stop-batch-3/stop",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "stopping"
