"""Visibility basics for /api/batches.

Until BACKEND-C lands the share tables, ``scope=shared`` should return
empty and ``scope=all`` should collapse to "mine" for non-admins. Admins
see everything with ``scope=all``.
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
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
    }


async def _mk_user_with_token(client, username: str) -> tuple[str, str]:
    """Register + login + mint reporter token; return (jwt, api_token)."""
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
async def test_batch_owner_is_set_from_posting_token(client):
    """Post a batch_start and read it back with owner_id populated."""
    import backend.db as db_mod
    from backend.models import Batch, User
    from sqlalchemy import select

    # Default client = tester's reporter token.
    r = await client.post("/api/events", json=_event("b-own"))
    assert r.status_code == 200

    async with db_mod.SessionLocal() as session:
        user = (
            await session.execute(
                select(User).where(User.username == "tester")
            )
        ).scalar_one()
        batch = await session.get(Batch, "b-own")
        assert batch.owner_id == user.id


@pytest.mark.asyncio
async def test_scope_mine_hides_other_users_batches(client):
    """User B can't see user A's batches under scope=mine."""
    # Tester (user A, also admin) posts a batch with her default token.
    assert (
        await client.post("/api/events", json=_event("a-batch"))
    ).status_code == 200

    # User B registers + mints a token + posts a different batch.
    bob_jwt, bob_token = await _mk_user_with_token(client, "bob")
    assert (
        await client.post(
            "/api/events",
            json=_event("b-batch"),
            headers={"Authorization": f"Bearer {bob_token}"},
        )
    ).status_code == 200

    # Bob viewing scope=mine sees only his batch.
    r = await client.get(
        "/api/batches?scope=mine",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    ids = {b["id"] for b in r.json()}
    assert ids == {"b-batch"}


@pytest.mark.asyncio
async def test_scope_all_non_admin_collapses_to_mine(client):
    """Without share tables, scope=all ≡ scope=mine for non-admins."""
    # Tester (admin) posts batch a-1.
    await client.post("/api/events", json=_event("a-1"))

    bob_jwt, bob_token = await _mk_user_with_token(client, "dave")
    await client.post(
        "/api/events",
        json=_event("d-1"),
        headers={"Authorization": f"Bearer {bob_token}"},
    )

    # Dave (non-admin) with scope=all sees only his own batch.
    r = await client.get(
        "/api/batches?scope=all",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    ids = {b["id"] for b in r.json()}
    assert ids == {"d-1"}


@pytest.mark.asyncio
async def test_scope_all_admin_sees_everything(client):
    """Admin user with scope=all sees every batch regardless of owner."""
    # Post batch as tester (who is admin because she was registered first).
    await client.post("/api/events", json=_event("admin-1"))

    # And one as a non-admin user.
    _, dave_token = await _mk_user_with_token(client, "dave")
    await client.post(
        "/api/events",
        json=_event("dave-1"),
        headers={"Authorization": f"Bearer {dave_token}"},
    )

    tester_jwt = getattr(client, "_test_default_jwt")
    r = await client.get(
        "/api/batches?scope=all",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    ids = {b["id"] for b in r.json()}
    assert {"admin-1", "dave-1"}.issubset(ids)


@pytest.mark.asyncio
async def test_scope_shared_is_empty_until_backend_c(client):
    """No share tables yet → scope=shared returns empty for everyone."""
    # Fill with a batch so "empty" is meaningful.
    await client.post("/api/events", json=_event("sole-1"))

    jwt = getattr(client, "_test_default_jwt")
    r = await client.get(
        "/api/batches?scope=shared",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_get_batch_detail_blocks_non_owner_non_admin(client):
    """Non-admin users can't fetch someone else's batch detail."""
    # User A (tester=admin) posts a batch.
    await client.post("/api/events", json=_event("admin-owned"))

    bob_jwt, _ = await _mk_user_with_token(client, "bob")
    # Bob can't see admin-owned batch.
    r = await client.get(
        "/api/batches/admin-owned",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    # 404 (hidden) not 403 to avoid confirming the batch exists.
    assert r.status_code == 404
