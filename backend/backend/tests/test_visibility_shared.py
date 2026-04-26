"""End-to-end visibility tests for the three scope modes.

Exercises the SQL composed by :class:`VisibilityResolver` once the
share tables actually have rows (BACKEND-C).
"""
from __future__ import annotations

import uuid

import pytest


def _event(batch_id: str, project: str = "proj") -> dict:
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
    await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
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
async def test_scope_mine_shows_only_owned(client):
    """Bob's scope=mine must not leak tester's or a shared batch."""
    await client.post("/api/events", json=_event("t-1"))  # tester
    bob_jwt, bob_token = await _mk_user(client, "bob")
    await client.post(
        "/api/events",
        json=_event("b-1"),
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    # Grant bob access to tester's t-1 — scope=mine must still hide it.
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post(
        "/api/batches/t-1/shares",
        json={"grantee_username": "bob", "permission": "viewer"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    r = await client.get(
        "/api/batches?scope=mine",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    ids = {b["id"] for b in r.json()}
    assert ids == {"b-1"}


@pytest.mark.asyncio
async def test_scope_shared_unions_batch_and_project_grants(client):
    """scope=shared returns union of batch_share and project_share."""
    # Tester posts two batches in two different projects.
    await client.post("/api/events", json=_event("bs-1", project="a"))
    await client.post("/api/events", json=_event("ps-1", project="b"))
    await client.post("/api/events", json=_event("ps-2", project="b"))

    bob_jwt, _ = await _mk_user(client, "bob")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Grant bob a single-batch share + a whole-project share.
    await client.post(
        "/api/batches/bs-1/shares",
        json={"grantee_username": "bob", "permission": "viewer"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    await client.post(
        "/api/projects/shares",
        json={
            "project": "b",
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
    assert ids == {"bs-1", "ps-1", "ps-2"}


@pytest.mark.asyncio
async def test_scope_all_admin_sees_everything(client):
    """Admin + scope=all ⇒ every non-deleted batch regardless of owner/share."""
    # Tester is admin; post a batch.
    await client.post("/api/events", json=_event("adm-a"))
    # Second user (non-admin) posts another.
    _, dave_token = await _mk_user(client, "dave")
    await client.post(
        "/api/events",
        json=_event("adm-b"),
        headers={"Authorization": f"Bearer {dave_token}"},
    )

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.get(
        "/api/batches?scope=all",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    ids = {b["id"] for b in r.json()}
    assert {"adm-a", "adm-b"}.issubset(ids)
