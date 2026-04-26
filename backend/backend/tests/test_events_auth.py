"""Auth gating on POST /api/events and /api/events/batch.

Covers:
  * no token → 401
  * em_view_ token → 403 (wrong scope)
  * em_live_ token → 200 + owner_id stamped on the created batch
  * JWT → 403 (reporter scope required)
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select


def _make_event(batch_id: str = "b-auth-1") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
    }


@pytest.mark.asyncio
async def test_post_event_without_token_is_401(unauthed_client):
    r = await unauthed_client.post("/api/events", json=_make_event())
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_event_with_viewer_token_is_403(client):
    jwt = getattr(client, "_test_default_jwt")
    cr = await client.post(
        "/api/tokens",
        json={"name": "viewer-only", "scope": "viewer"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    viewer_token = cr.json()["token"]

    r = await client.post(
        "/api/events",
        json=_make_event(),
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert r.status_code == 403
    assert "reporter" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_post_event_with_jwt_is_403(client):
    """JWT carries web-session identity but must not act as reporter."""
    jwt = getattr(client, "_test_default_jwt")
    r = await client.post(
        "/api/events",
        json=_make_event(),
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_post_event_stamps_owner_id(client):
    """First event to a new batch records the token owner's id."""
    import backend.db as db_mod
    from backend.models import Batch, User

    # The default client is using tester's em_live_ token.
    r = await client.post("/api/events", json=_make_event("b-owner-1"))
    assert r.status_code == 200, r.text

    async with db_mod.SessionLocal() as session:
        user = (
            await session.execute(
                select(User).where(User.username == "tester")
            )
        ).scalar_one()
        batch = await session.get(Batch, "b-owner-1")
        assert batch is not None
        assert batch.owner_id == user.id


@pytest.mark.asyncio
async def test_owner_id_is_not_overwritten_by_second_token(client):
    """A second token on the same batch must not change owner_id."""
    import backend.db as db_mod
    from backend.models import Batch, User

    batch_id = "b-owner-lock"
    # First event via the default tester token.
    r1 = await client.post("/api/events", json=_make_event(batch_id))
    assert r1.status_code == 200

    async with db_mod.SessionLocal() as session:
        tester = (
            await session.execute(
                select(User).where(User.username == "tester")
            )
        ).scalar_one()
        original_owner = (await session.get(Batch, batch_id)).owner_id
        assert original_owner == tester.id

    # Register bob + mint his reporter token + post again against the same batch.
    await client.post(
        "/api/auth/register",
        json={
            "username": "charlie",
            "email": "charlie@example.com",
            "password": "password123",
        },
    )
    charlie_login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "charlie", "password": "password123"},
    )
    charlie_jwt = charlie_login.json()["access_token"]
    tok_resp = await client.post(
        "/api/tokens",
        json={"name": "charlie-reporter", "scope": "reporter"},
        headers={"Authorization": f"Bearer {charlie_jwt}"},
    )
    charlie_token = tok_resp.json()["token"]

    # Post from charlie's token; owner_id must remain tester's.
    ev2 = _make_event(batch_id)
    ev2["event_type"] = "resource_snapshot"
    ev2["timestamp"] = "2026-04-23T10:00:00Z"
    ev2["source"] = {"project": "p", "host": "h1"}
    ev2["data"] = {"gpu_util_pct": 10}
    r2 = await client.post(
        "/api/events",
        json=ev2,
        headers={"Authorization": f"Bearer {charlie_token}"},
    )
    assert r2.status_code == 200

    async with db_mod.SessionLocal() as session:
        batch = await session.get(Batch, batch_id)
        assert batch.owner_id == original_owner  # unchanged


@pytest.mark.asyncio
async def test_events_batch_requires_reporter_token(unauthed_client):
    """The bulk endpoint enforces the same auth as singleton POST."""
    r = await unauthed_client.post(
        "/api/events/batch",
        json={"events": [_make_event("b-bulk")]},
    )
    assert r.status_code == 401
