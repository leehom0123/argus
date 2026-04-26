"""Project-level sharing.

A project share covers every batch that the owner has under that
project, including future batches uploaded after the share was granted.
"""
from __future__ import annotations

import uuid

import pytest


def _event(batch_id: str, project: str = "my-proj") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": project},
        "data": {"n_total_jobs": 1},
    }


async def _mk_user(client, username: str) -> tuple[str, str]:
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201
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
async def test_project_share_covers_existing_batches(client):
    """Sharing project P should grant visibility on every existing P batch."""
    # Tester posts two batches under my-proj.
    await client.post("/api/events", json=_event("p-1", project="my-proj"))
    await client.post("/api/events", json=_event("p-2", project="my-proj"))
    # And a control batch under other-proj.
    await client.post(
        "/api/events", json=_event("o-1", project="other-proj")
    )

    bob_jwt, _ = await _mk_user(client, "bob")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.post(
        "/api/projects/shares",
        json={
            "project": "my-proj",
            "grantee_username": "bob",
            "permission": "viewer",
        },
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201, r.text

    r = await client.get(
        "/api/batches?scope=shared",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    ids = {b["id"] for b in r.json()}
    assert ids == {"p-1", "p-2"}


@pytest.mark.asyncio
async def test_project_share_covers_future_batches(client):
    """A batch uploaded after the share is granted must still be visible."""
    bob_jwt, _ = await _mk_user(client, "bob")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Share first, then upload.
    await client.post(
        "/api/projects/shares",
        json={
            "project": "my-proj",
            "grantee_username": "bob",
            "permission": "viewer",
        },
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    # New batch after the grant — still needs to surface for Bob.
    await client.post("/api/events", json=_event("future-1", project="my-proj"))

    r = await client.get(
        "/api/batches?scope=shared",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    ids = {b["id"] for b in r.json()}
    assert "future-1" in ids


@pytest.mark.asyncio
async def test_project_share_doesnt_leak_other_projects(client):
    """other-proj should stay hidden from the grantee."""
    await client.post("/api/events", json=_event("my-1", project="my-proj"))
    await client.post(
        "/api/events", json=_event("other-1", project="other-proj")
    )

    bob_jwt, _ = await _mk_user(client, "bob")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post(
        "/api/projects/shares",
        json={
            "project": "my-proj",
            "grantee_username": "bob",
            "permission": "viewer",
        },
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )

    r = await client.get(
        "/api/batches?scope=shared",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    ids = {b["id"] for b in r.json()}
    assert "other-1" not in ids
    assert "my-1" in ids


@pytest.mark.asyncio
async def test_project_share_list_and_revoke(client):
    """Owner can list + revoke their outgoing project shares."""
    await client.post("/api/events", json=_event("p-r-1", project="my-proj"))
    bob_jwt, _ = await _mk_user(client, "bob")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Add
    r = await client.post(
        "/api/projects/shares",
        json={
            "project": "my-proj",
            "grantee_username": "bob",
            "permission": "viewer",
        },
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201
    grantee_id = r.json()["grantee_id"]

    # List
    r = await client.get(
        "/api/projects/shares",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["project"] == "my-proj"

    # Revoke
    r = await client.delete(
        f"/api/projects/shares/my-proj/{grantee_id}",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200

    # Bob can no longer see the batch.
    r = await client.get(
        "/api/batches?scope=shared",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.json() == []
