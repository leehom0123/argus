"""Tests for ``/api/auth/sessions`` — list + revoke (issue #31)."""
from __future__ import annotations

import time

import pytest


async def _register_and_login(client, username: str = "alice") -> str:
    """Register + login ``username``. Returns the JWT."""
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
        headers={"user-agent": f"pytest/{username}"},
    )
    assert lr.status_code == 200, lr.text
    return lr.json()["access_token"]


@pytest.mark.asyncio
async def test_list_sessions_returns_current_only(client):
    """A fresh login appears in ``/api/auth/sessions`` with is_current=True."""
    jwt = await _register_and_login(client, "alice")
    r = await client.get(
        "/api/auth/sessions",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["is_current"] is True
    assert row["user_agent"] == "pytest/alice"
    # Required fields present.
    for key in ("jti", "issued_at", "expires_at", "last_seen_at"):
        assert row[key], f"missing {key}"


@pytest.mark.asyncio
async def test_list_sessions_multiple_logins_tagged_correctly(client):
    """Two logins → two rows; only the one driving the request is current."""
    jwt_a = await _register_and_login(client, "alice")
    # Second login for the same user (different UA).
    lr2 = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
        headers={"user-agent": "pytest/alice-device-2"},
    )
    assert lr2.status_code == 200
    jwt_b = lr2.json()["access_token"]
    assert jwt_b != jwt_a

    # Call sessions using jwt_a → that one is current.
    r = await client.get(
        "/api/auth/sessions",
        headers={"Authorization": f"Bearer {jwt_a}"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    by_ua = {r["user_agent"]: r for r in rows}
    assert by_ua["pytest/alice"]["is_current"] is True
    assert by_ua["pytest/alice-device-2"]["is_current"] is False

    # Now the same call via jwt_b flips which one is current.
    r = await client.get(
        "/api/auth/sessions",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    rows = r.json()
    by_ua = {r["user_agent"]: r for r in rows}
    assert by_ua["pytest/alice-device-2"]["is_current"] is True
    assert by_ua["pytest/alice"]["is_current"] is False


@pytest.mark.asyncio
async def test_revoke_session_blocks_further_requests(client):
    """Revoking a jti → subsequent requests with that JWT return 401."""
    jwt_a = await _register_and_login(client, "alice")
    # Second login so we have a separate JWT to drive the revoke.
    lr2 = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
        headers={"user-agent": "pytest/alice-device-2"},
    )
    jwt_b = lr2.json()["access_token"]

    # Get the jti for jwt_a from the sessions list (looked up via jwt_b).
    r = await client.get(
        "/api/auth/sessions",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    rows = r.json()
    target = next(r for r in rows if r["user_agent"] == "pytest/alice")
    jti = target["jti"]

    # Revoke from jwt_b.
    rv = await client.post(
        f"/api/auth/sessions/{jti}/revoke",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    assert rv.status_code == 200, rv.text

    # jwt_a must now fail auth.
    r = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {jwt_a}"},
    )
    assert r.status_code == 401

    # jwt_b must still work.
    r = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    assert r.status_code == 200

    # Revoked session must no longer appear in the list (we filter
    # revoked_at != NULL).
    r = await client.get(
        "/api/auth/sessions",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    rows = r.json()
    assert all(r["jti"] != jti for r in rows)


@pytest.mark.asyncio
async def test_revoke_other_users_session_404(client):
    """A user cannot revoke another user's jti."""
    # alice is the default fixture user (via register); create bob separately.
    jwt_a = await _register_and_login(client, "alice")
    jwt_b = await _register_and_login(client, "bob")

    # Find alice's jti using her own jwt.
    r = await client.get(
        "/api/auth/sessions",
        headers={"Authorization": f"Bearer {jwt_a}"},
    )
    alice_jti = r.json()[0]["jti"]

    # Bob tries to revoke alice's session → 404 (not 403, to avoid
    # leaking existence).
    rv = await client.post(
        f"/api/auth/sessions/{alice_jti}/revoke",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    assert rv.status_code == 404
    # Alice's JWT still works.
    r = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {jwt_a}"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_sessions_requires_auth(unauthed_client):
    r = await unauthed_client.get("/api/auth/sessions")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_revoke_unknown_jti_404(client):
    jwt = await _register_and_login(client, "alice")
    r = await client.post(
        "/api/auth/sessions/does-not-exist/revoke",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_last_seen_is_bumped(client):
    """Calling any authed endpoint updates last_seen_at."""
    jwt = await _register_and_login(client, "alice")
    r1 = await client.get(
        "/api/auth/sessions",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    first = r1.json()[0]["last_seen_at"]
    # Force a new wall-clock second; our timestamp resolution is 1s.
    time.sleep(1.05)
    # Hit another authed endpoint to bump last_seen.
    await client.get("/api/auth/me", headers={"Authorization": f"Bearer {jwt}"})
    r2 = await client.get(
        "/api/auth/sessions",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    second = r2.json()[0]["last_seen_at"]
    assert second >= first  # monotonic
    assert second != first  # actually changed
