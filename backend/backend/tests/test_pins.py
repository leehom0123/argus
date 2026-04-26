"""Tests for ``/api/pins`` — per-user compare-pool."""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import (
    mk_user_with_token,
    post_event,
    make_batch_start,
    seed_completed_batch,
)


@pytest.mark.asyncio
async def test_pin_roundtrip(client):
    """POST → GET → DELETE → GET."""
    await seed_completed_batch(client, batch_id="b-1")

    r = await client.post("/api/pins", json={"batch_id": "b-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == "b-1"
    assert body["project"] == "deepts"
    assert body["pinned_at"]

    r = await client.get("/api/pins")
    assert r.status_code == 200
    assert any(p["batch_id"] == "b-1" for p in r.json())

    r = await client.delete("/api/pins/b-1")
    assert r.status_code == 204

    r = await client.get("/api/pins")
    assert not any(p["batch_id"] == "b-1" for p in r.json())


@pytest.mark.asyncio
async def test_pin_is_idempotent(client):
    """Re-POSTing the same pin returns 200, same row."""
    await seed_completed_batch(client, batch_id="b-1")
    r1 = await client.post("/api/pins", json={"batch_id": "b-1"})
    assert r1.status_code == 200
    first_ts = r1.json()["pinned_at"]

    r2 = await client.post("/api/pins", json={"batch_id": "b-1"})
    assert r2.status_code == 200
    assert r2.json()["pinned_at"] == first_ts

    r = await client.get("/api/pins")
    assert sum(1 for p in r.json() if p["batch_id"] == "b-1") == 1


@pytest.mark.asyncio
async def test_pin_cap_at_four(client):
    """5th POST → 400 'unpin one first'."""
    for i in range(4):
        await seed_completed_batch(client, batch_id=f"b-{i}")
        r = await client.post("/api/pins", json={"batch_id": f"b-{i}"})
        assert r.status_code == 200, (i, r.text)

    # 5th batch exists + is visible, but the cap kicks in.
    await seed_completed_batch(client, batch_id="b-4")
    r = await client.post("/api/pins", json={"batch_id": "b-4"})
    assert r.status_code == 400
    assert "unpin" in r.json()["detail"].lower()

    # After unpinning one, the 5th can pin.
    r = await client.delete("/api/pins/b-0")
    assert r.status_code == 204
    r = await client.post("/api/pins", json={"batch_id": "b-4"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_pin_missing_batch_404(client):
    """Pinning a non-existent batch returns 404."""
    r = await client.post("/api/pins", json={"batch_id": "nope"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_pin_blocked_for_non_visible_batch(client):
    """Bob can't pin Alice's private batch."""
    await seed_completed_batch(client, batch_id="admin-only")

    bob_jwt, _ = await mk_user_with_token(client, "bob")
    r = await client.post(
        "/api/pins",
        json={"batch_id": "admin-only"},
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_pins_are_per_user(client):
    """Alice's pins don't show up for Bob."""
    await seed_completed_batch(client, batch_id="b-alice")
    r = await client.post("/api/pins", json={"batch_id": "b-alice"})
    assert r.status_code == 200

    bob_jwt, _ = await mk_user_with_token(client, "bob")
    r = await client.get(
        "/api/pins", headers={"Authorization": f"Bearer {bob_jwt}"}
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_delete_missing_pin_is_noop(client):
    """Unpinning a non-existent pin → 204."""
    r = await client.delete("/api/pins/never-pinned")
    assert r.status_code == 204
