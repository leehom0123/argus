"""Admin endpoints: users, feature flags, audit log."""
from __future__ import annotations

import pytest


async def _mk_user(client, username: str) -> str:
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
    return lr.json()["access_token"]


@pytest.mark.asyncio
async def test_non_admin_cannot_hit_admin_endpoints(client):
    """Any /api/admin/* returns 403 for non-admin users."""
    bob_jwt = await _mk_user(client, "bob")
    for path in (
        "/api/admin/users",
        "/api/admin/feature-flags",
        "/api/admin/audit-log",
    ):
        r = await client.get(
            path, headers={"Authorization": f"Bearer {bob_jwt}"}
        )
        assert r.status_code == 403, f"{path} returned {r.status_code}"


@pytest.mark.asyncio
async def test_admin_lists_all_users(client):
    """Admin (tester, first user) sees every registered account."""
    await _mk_user(client, "bob")
    await _mk_user(client, "dave")

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200
    names = {u["username"] for u in r.json()}
    assert {"tester", "bob", "dave"}.issubset(names)


@pytest.mark.asyncio
async def test_ban_prevents_login(client):
    """After admin bans a user, login returns 401."""
    bob_jwt = await _mk_user(client, "bob")

    # Look up Bob's user id via admin listing.
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    users = (
        await client.get(
            "/api/admin/users",
            headers={"Authorization": f"Bearer {tester_jwt}"},
        )
    ).json()
    bob_id = next(u["id"] for u in users if u["username"] == "bob")

    r = await client.post(
        f"/api/admin/users/{bob_id}/ban",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200
    assert r.json()["is_active"] is False

    # Banned user can't re-login.
    r = await client.post(
        "/api/auth/login",
        json={"username_or_email": "bob", "password": "password123"},
    )
    assert r.status_code == 401

    # Banned user's existing JWT also fails at /me (is_active gate).
    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {bob_jwt}"}
    )
    assert r.status_code == 401

    # Unban restores access.
    r = await client.post(
        f"/api/admin/users/{bob_id}/unban",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200
    r = await client.post(
        "/api/auth/login",
        json={"username_or_email": "bob", "password": "password123"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_feature_flag_read_write(client):
    """Admin can read defaults, write overrides, and read back the new value."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    headers = {"Authorization": f"Bearer {tester_jwt}"}

    # Defaults visible.
    r = await client.get("/api/admin/feature-flags", headers=headers)
    assert r.status_code == 200
    flags = {f["key"]: f for f in r.json()}
    assert flags["registration_open"]["value"] is True
    assert flags["stalled_threshold_sec"]["value"] == 300

    # Flip registration_open.
    r = await client.put(
        "/api/admin/feature-flags/registration_open",
        json={"value": False},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["value"] is False

    # Read-back.
    r = await client.get("/api/admin/feature-flags", headers=headers)
    flags = {f["key"]: f for f in r.json()}
    assert flags["registration_open"]["value"] is False
    assert flags["registration_open"]["updated_by"] is not None

    # And now registration is refused.
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "denied",
            "email": "denied@example.com",
            "password": "password123",
        },
    )
    assert r.status_code == 403
