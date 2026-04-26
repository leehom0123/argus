"""Tests for ``POST /api/auth/change-password`` (Team Pwd).

Covers:
  * happy path — new password persisted, old password rejected at login,
    other sessions revoked, current caller's JWT stays valid
  * wrong current password → 401
  * same new == current → 400
  * pydantic validation on too-short new password → 422
  * isolation: other users' JWTs are NOT revoked
  * rate limit: 5 per hour per user_id → 6th attempt is 429
  * email notification fires
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.models import ActiveSession, User


async def _register(client, *, username: str, email: str, password: str):
    """Register a user (does NOT log them in)."""
    r = await client.post(
        "/api/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client, *, username_or_email: str, password: str) -> str:
    """Return a JWT access token."""
    r = await client.post(
        "/api/auth/login",
        json={
            "username_or_email": username_or_email,
            "password": password,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_happy_path_revokes_other_sessions(
    client, email_service
):
    """Happy path: the caller's own JWT stays valid but sibling JWTs get
    ``revoked_at`` stamped."""
    # Register alice (second user — default ``tester`` is the pre-seeded first user).
    await _register(
        client,
        username="alice",
        email="alice@example.com",
        password="password123",
    )

    # Two independent logins create two JWTs → two active_sessions rows.
    jwt_current = await _login(
        client, username_or_email="alice", password="password123"
    )
    jwt_other = await _login(
        client, username_or_email="alice", password="password123"
    )

    email_service.sent_messages.clear()

    # Use jwt_current as the caller.
    r = await client.post(
        "/api/auth/change-password",
        json={
            "current_password": "password123",
            "new_password": "newpass9876",
        },
        headers={"Authorization": f"Bearer {jwt_current}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # Caller's own JWT is still valid — ``GET /api/auth/me`` succeeds.
    me = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {jwt_current}"},
    )
    assert me.status_code == 200

    # The sibling JWT (jwt_other) is DB-revoked and no longer works.
    revoked_me = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {jwt_other}"},
    )
    assert revoked_me.status_code == 401

    # Confirm active_sessions rows match expectations BEFORE re-login,
    # otherwise the verification login below would create a fresh row
    # that obscures the revocation bookkeeping.
    import backend.db as db_mod

    async with db_mod.SessionLocal() as session:
        alice = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        sessions = (
            await session.execute(
                select(ActiveSession).where(ActiveSession.user_id == alice.id)
            )
        ).scalars().all()
        assert len(sessions) == 2
        revoked = [s for s in sessions if s.revoked_at is not None]
        live = [s for s in sessions if s.revoked_at is None]
        assert len(revoked) == 1
        assert len(live) == 1

    # Old password no longer works at login.
    bad = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    assert bad.status_code == 401

    # New password works.
    good = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "newpass9876"},
    )
    assert good.status_code == 200

    # Email was fired.
    pw_emails = [
        m
        for m in email_service.sent_messages
        if m.template == "<password_changed-inline>"
    ]
    assert len(pw_emails) == 1
    assert pw_emails[0].to == "alice@example.com"


# ---------------------------------------------------------------------------
# Error surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_wrong_current_returns_401(client):
    await _register(
        client,
        username="bob",
        email="bob@example.com",
        password="password123",
    )
    jwt = await _login(
        client, username_or_email="bob", password="password123"
    )

    r = await client.post(
        "/api/auth/change-password",
        json={
            "current_password": "WRONG-password",
            "new_password": "newpass9876",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 401, r.text
    # Old password still works after failed attempt.
    verify = await client.post(
        "/api/auth/login",
        json={"username_or_email": "bob", "password": "password123"},
    )
    assert verify.status_code == 200


@pytest.mark.asyncio
async def test_change_password_same_as_current_returns_400(client):
    await _register(
        client,
        username="carol",
        email="carol@example.com",
        password="password123",
    )
    jwt = await _login(
        client, username_or_email="carol", password="password123"
    )

    r = await client.post(
        "/api/auth/change-password",
        json={
            "current_password": "password123",
            "new_password": "password123",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_change_password_too_short_rejected_by_pydantic(client):
    await _register(
        client,
        username="dave",
        email="dave@example.com",
        password="password123",
    )
    jwt = await _login(
        client, username_or_email="dave", password="password123"
    )

    r = await client.post(
        "/api/auth/change-password",
        json={
            "current_password": "password123",
            "new_password": "short1",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    # min_length=10 on Pydantic Field → 422
    assert r.status_code in (400, 422), r.text


@pytest.mark.asyncio
async def test_change_password_unauthed_rejected(unauthed_client):
    """No Authorization header → 401 (never reaches the handler)."""
    r = await unauthed_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "password123",
            "new_password": "newpass9876",
        },
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_change_password_rejects_api_token(client):
    """API tokens (``em_live_*`` / ``em_view_*``) must not be able to call
    change-password — the endpoint is web-session-only so a stolen reporter
    token on a training box can't flip a user's password.

    The default client fixture already sends an ``em_live_*`` token on every
    request (see conftest). Without the ``require_web_session`` guard this
    endpoint would accept it; with the guard it must 403.
    """
    # Default client fixture auth is the reporter API token, not a JWT.
    r = await client.post(
        "/api/auth/change-password",
        json={
            "current_password": "password123",
            "new_password": "newpass9876",
        },
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_does_not_revoke_other_users_sessions(client):
    """Alice changing her password must not touch bob's JWTs."""
    await _register(
        client,
        username="alice",
        email="alice@example.com",
        password="password123",
    )
    await _register(
        client,
        username="bob",
        email="bob@example.com",
        password="password123",
    )

    jwt_alice = await _login(
        client, username_or_email="alice", password="password123"
    )
    jwt_bob = await _login(
        client, username_or_email="bob", password="password123"
    )

    r = await client.post(
        "/api/auth/change-password",
        json={
            "current_password": "password123",
            "new_password": "newpass9876",
        },
        headers={"Authorization": f"Bearer {jwt_alice}"},
    )
    assert r.status_code == 200

    # Bob's JWT must STILL work.
    me_bob = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {jwt_bob}"},
    )
    assert me_bob.status_code == 200
    assert me_bob.json()["username"] == "bob"


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_rate_limit_kicks_in_on_sixth_attempt(client):
    """5 attempts in an hour allowed, 6th must 429.

    We feed wrong-password attempts because they don't mutate state but
    still debit the rate bucket (bucket is checked *before* current-
    password verification).
    """
    await _register(
        client,
        username="eve",
        email="eve@example.com",
        password="password123",
    )
    jwt = await _login(
        client, username_or_email="eve", password="password123"
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    body = {
        "current_password": "WRONG-password",
        "new_password": "newpass9876",
    }

    # 5 wrong-password attempts → each 401 (allowed past the rate limit).
    for _ in range(5):
        r = await client.post(
            "/api/auth/change-password", json=body, headers=headers
        )
        assert r.status_code == 401, r.text

    # 6th in the same hour must 429.
    r6 = await client.post(
        "/api/auth/change-password", json=body, headers=headers
    )
    assert r6.status_code == 429, r6.text
    assert "Retry-After" in r6.headers


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_sends_email_notification(
    client, email_service, monkeypatch
):
    """Assert the email service method is called exactly once after a
    successful change."""
    await _register(
        client,
        username="frank",
        email="frank@example.com",
        password="password123",
    )
    jwt = await _login(
        client, username_or_email="frank", password="password123"
    )

    calls: list[dict] = []
    original = email_service.send_password_changed_notification

    async def _spy(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        email_service, "send_password_changed_notification", _spy
    )

    r = await client.post(
        "/api/auth/change-password",
        json={
            "current_password": "password123",
            "new_password": "newpass9876",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    # Kwargs carry the callee's email + locale + IP + UA.
    assert calls[0]["kwargs"]["to"] == "frank@example.com"
    assert "ip" in calls[0]["kwargs"]
    assert "user_agent" in calls[0]["kwargs"]
