"""Batch-level sharing.

Covers: owner can add share, grantee sees batch under scope=shared,
non-owner can't add a share, non-grantee can't read, revoke round-trip.
"""
from __future__ import annotations

import uuid

import pytest


def _event(batch_id: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "proj"},
        "data": {"n_total_jobs": 1},
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
async def test_owner_can_add_share_and_grantee_sees_it(client):
    """Happy path: tester (owner) shares b1 with bob → bob/scope=shared has b1."""
    # Tester posts a batch
    r = await client.post("/api/events", json=_event("b1"))
    assert r.status_code == 200

    bob_jwt, _ = await _mk_user(client, "bob")

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/batches/b1/shares",
        json={"grantee_username": "bob", "permission": "viewer"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["grantee_username"] == "bob"
    assert body["permission"] == "viewer"

    # Bob sees b1 under scope=shared
    r = await client.get(
        "/api/batches?scope=shared",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    ids = {b["id"] for b in r.json()}
    assert ids == {"b1"}

    # And can fetch detail directly
    r = await client.get(
        "/api/batches/b1",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_non_owner_cannot_add_share(client):
    """Bob can't share tester's batch since he doesn't own it."""
    await client.post("/api/events", json=_event("b2"))
    bob_jwt, _ = await _mk_user(client, "bob")

    r = await client.post(
        "/api/batches/b2/shares",
        json={"grantee_username": "bob", "permission": "viewer"},
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    # Must be rejected — either 403 or 404; we pick 403 since bob's seen
    # the batch id in a URL so hiding existence doesn't help.
    assert r.status_code in (403, 404)


@pytest.mark.asyncio
async def test_non_grantee_still_blocked(client):
    """A second non-owner user can't peek at tester's batch even after share to someone else."""
    await client.post("/api/events", json=_event("b3"))
    bob_jwt, _ = await _mk_user(client, "bob")
    carol_jwt, _ = await _mk_user(client, "carol")

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    # Share only with Bob.
    await client.post(
        "/api/batches/b3/shares",
        json={"grantee_username": "bob", "permission": "viewer"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )

    # Carol sees nothing.
    r = await client.get(
        "/api/batches?scope=shared",
        headers={"Authorization": f"Bearer {carol_jwt}"},
    )
    assert r.json() == []

    r = await client.get(
        "/api/batches/b3",
        headers={"Authorization": f"Bearer {carol_jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cannot_share_with_self(client):
    """Owner sharing to themselves returns 400."""
    await client.post("/api/events", json=_event("b4"))
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.post(
        "/api/batches/b4/shares",
        json={"grantee_username": "tester", "permission": "viewer"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_revoke_share_removes_access(client):
    """After DELETE, grantee no longer sees the batch."""
    await client.post("/api/events", json=_event("b5"))
    bob_jwt, _ = await _mk_user(client, "bob")

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/batches/b5/shares",
        json={"grantee_username": "bob", "permission": "viewer"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201
    grantee_id = r.json()["grantee_id"]

    # Confirm visible
    r = await client.get(
        "/api/batches/b5",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 200

    # Revoke
    r = await client.delete(
        f"/api/batches/b5/shares/{grantee_id}",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200

    # Now gone
    r = await client.get(
        "/api/batches/b5",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_share_list_shows_grantees(client):
    """Owner can list current shares and see grantee usernames."""
    await client.post("/api/events", json=_event("b6"))
    await _mk_user(client, "bob")
    await _mk_user(client, "dave")

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post(
        "/api/batches/b6/shares",
        json={"grantee_username": "bob", "permission": "viewer"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    await client.post(
        "/api/batches/b6/shares",
        json={"grantee_username": "dave", "permission": "editor"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )

    r = await client.get(
        "/api/batches/b6/shares",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert {row["grantee_username"] for row in rows} == {"bob", "dave"}
    perms = {row["grantee_username"]: row["permission"] for row in rows}
    assert perms == {"bob": "viewer", "dave": "editor"}
