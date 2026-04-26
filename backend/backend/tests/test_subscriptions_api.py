"""Team Email — per-user subscription API."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _jwt_headers(client):
    return {"Authorization": f"Bearer {client._test_default_jwt}"}


async def test_list_subscriptions_empty(client):
    r = await client.get(
        "/api/me/subscriptions", headers=await _jwt_headers(client)
    )
    assert r.status_code == 200
    assert r.json() == []


async def test_patch_upsert_subscriptions(client):
    r = await client.patch(
        "/api/me/subscriptions",
        headers=await _jwt_headers(client),
        json={
            "subscriptions": [
                {"project": None, "event_type": "batch_done", "enabled": True},
                {"project": "my-proj", "event_type": "batch_failed", "enabled": False},
            ]
        },
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2
    by_key = {(row["project"], row["event_type"]): row["enabled"] for row in rows}
    assert by_key[(None, "batch_done")] is True
    assert by_key[("my-proj", "batch_failed")] is False

    # Flipping values updates rather than duplicates.
    r2 = await client.patch(
        "/api/me/subscriptions",
        headers=await _jwt_headers(client),
        json={
            "subscriptions": [
                {"project": None, "event_type": "batch_done", "enabled": False},
            ]
        },
    )
    assert r2.status_code == 200
    rows2 = r2.json()
    # Still 2 rows — one flipped, one untouched
    assert len(rows2) == 2
    by_key = {(row["project"], row["event_type"]): row["enabled"] for row in rows2}
    assert by_key[(None, "batch_done")] is False


async def test_patch_rejects_unknown_event_type(client):
    r = await client.patch(
        "/api/me/subscriptions",
        headers=await _jwt_headers(client),
        json={
            "subscriptions": [
                {"project": None, "event_type": "garbage_event", "enabled": True},
            ]
        },
    )
    assert r.status_code == 400
    assert "garbage_event" in r.json()["detail"]


async def test_patch_requires_auth(client):
    client.headers.pop("Authorization", None)
    r = await client.patch(
        "/api/me/subscriptions",
        json={"subscriptions": []},
    )
    assert r.status_code == 401
