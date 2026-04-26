"""Login + lockout tests."""
from __future__ import annotations

import pytest


async def _register(client, username="alice", email="alice@example.com"):
    r = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email,
            "password": "password123",
        },
    )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_login_by_username(client):
    await _register(client)
    r = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["username"] == "alice"


@pytest.mark.asyncio
async def test_login_by_email(client):
    await _register(client)
    r = await client.post(
        "/api/auth/login",
        json={
            "username_or_email": "alice@example.com",
            "password": "password123",
        },
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_login_wrong_password_401(client):
    await _register(client)
    r = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "wrongpass123"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_user_401(client):
    r = await client.post(
        "/api/auth/login",
        json={"username_or_email": "ghost", "password": "password123"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_lockout_after_5_failures(client):
    await _register(client)
    for _ in range(5):
        r = await client.post(
            "/api/auth/login",
            json={"username_or_email": "alice", "password": "wrongpass123"},
        )
        assert r.status_code == 401

    # 6th attempt with correct password should now be locked.
    r = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    assert r.status_code == 423
    assert "Retry-After" in r.headers


@pytest.mark.asyncio
async def test_lockout_reset_after_window(client, monkeypatch):
    """After the lock window passes, login should succeed again.

    We manipulate the ``locked_until`` value directly to simulate wall-clock
    advance — simpler than monkeypatching datetime everywhere.
    """
    from sqlalchemy import select

    import backend.db as db_mod
    from backend.models import User

    await _register(client)
    for _ in range(5):
        await client.post(
            "/api/auth/login",
            json={"username_or_email": "alice", "password": "wrongpass123"},
        )

    # Sanity: the account is locked.
    r = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    assert r.status_code == 423

    # Rewind locked_until into the past.
    async with db_mod.SessionLocal() as session:
        u = (
            await session.execute(
                select(User).where(User.username == "alice")
            )
        ).scalar_one()
        u.locked_until = "2000-01-01T00:00:00Z"
        await session.commit()

    r2 = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    assert r2.status_code == 200, r2.text


@pytest.mark.asyncio
async def test_me_requires_auth(unauthed_client):
    r = await unauthed_client.get("/api/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_rejects_malformed_bearer(unauthed_client):
    r = await unauthed_client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_logout_blacklists_token(client):
    await _register(client)
    r = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # First /me works
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200

    # Logout
    r2 = await client.post("/api/auth/logout", headers=headers)
    assert r2.status_code == 200

    # Same token can no longer be used.
    assert (
        await client.get("/api/auth/me", headers=headers)
    ).status_code == 401
