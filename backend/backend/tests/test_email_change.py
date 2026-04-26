"""Email-change flow tests.

Covers the new ``POST /api/auth/change-email`` + ``GET /api/auth/verify-new-email``
endpoints. The flow reuses the ``email_verification`` table with
``kind='email_change'`` and binds the new email to the token via the
new ``payload`` column added in migration 022.

Test matrix mirrors the spec for Task D:
  * wrong current password           → 401
  * right password + valid email     → 200, token in outbox
  * verify token                     → email updates
  * replay token                     → 410
  * expired token                    → 410
  * same email as current            → 400
"""
from __future__ import annotations

import pytest
from sqlalchemy import select


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_and_login(client, *, username: str, email: str, password: str) -> str:
    """Register + login a fresh user; return the bearer JWT.

    The default conftest ``client`` fixture already auto-registers
    ``tester@example.com``; for these tests we want to drive the flow
    from a non-default account so we don't collide with the bootstrap
    user when asserting on email-uniqueness paths.
    """
    r = await client.post(
        "/api/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    assert r.status_code == 201, r.text
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": password},
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _change_email_token_from_outbox(email_service, *, to: str) -> str:
    """Return the most recent email-change verify token mailed to ``to``."""
    sent = [
        m
        for m in email_service.sent_messages
        if m.template == "<email_change-inline>" and m.to == to
    ]
    assert sent, f"no email-change verification mailed to {to}"
    return sent[-1].context["verify_url"].split("token=")[-1]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_email_wrong_password_returns_401(client, email_service):
    jwt = await _register_and_login(
        client,
        username="alice",
        email="alice@example.com",
        password="password123",
    )
    email_service.sent_messages.clear()

    r = await client.post(
        "/api/auth/change-email",
        json={
            "new_email": "alice-new@example.com",
            "current_password": "WRONG-pw-99",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 401, r.text
    # No email should leak when the password check fails.
    assert not [
        m for m in email_service.sent_messages
        if m.template == "<email_change-inline>"
    ]


@pytest.mark.asyncio
async def test_change_email_success_issues_token_and_mails(client, email_service):
    jwt = await _register_and_login(
        client,
        username="alice",
        email="alice@example.com",
        password="password123",
    )
    email_service.sent_messages.clear()

    r = await client.post(
        "/api/auth/change-email",
        json={
            "new_email": "alice-new@example.com",
            "current_password": "password123",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text

    sent = [
        m for m in email_service.sent_messages
        if m.template == "<email_change-inline>"
    ]
    assert len(sent) == 1
    # Mail goes to the *new* address, not the current one.
    assert sent[0].to == "alice-new@example.com"
    assert "token=" in sent[0].context["verify_url"]

    # The User row is unchanged at this point — verification hasn't
    # happened yet.
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_verify_new_email_updates_user_email(client, email_service):
    jwt = await _register_and_login(
        client,
        username="alice",
        email="alice@example.com",
        password="password123",
    )
    email_service.sent_messages.clear()

    await client.post(
        "/api/auth/change-email",
        json={
            "new_email": "alice-new@example.com",
            "current_password": "password123",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    token = _change_email_token_from_outbox(
        email_service, to="alice-new@example.com"
    )

    r = await client.get("/api/auth/verify-new-email", params={"token": token})
    assert r.status_code == 200, r.text

    # User row now has the new email + email_verified flipped to True.
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "alice-new@example.com"
    assert body["email_verified"] is True


@pytest.mark.asyncio
async def test_verify_replay_returns_410(client, email_service):
    jwt = await _register_and_login(
        client,
        username="alice",
        email="alice@example.com",
        password="password123",
    )
    email_service.sent_messages.clear()
    await client.post(
        "/api/auth/change-email",
        json={
            "new_email": "alice-new@example.com",
            "current_password": "password123",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    token = _change_email_token_from_outbox(
        email_service, to="alice-new@example.com"
    )

    first = await client.get("/api/auth/verify-new-email", params={"token": token})
    assert first.status_code == 200

    replay = await client.get("/api/auth/verify-new-email", params={"token": token})
    assert replay.status_code == 410, replay.text


@pytest.mark.asyncio
async def test_verify_expired_token_returns_410(client, email_service):
    """Force the token's expires_at into the past; it must surface as 410."""
    import backend.db as db_mod
    from backend.models import EmailVerification

    jwt = await _register_and_login(
        client,
        username="alice",
        email="alice@example.com",
        password="password123",
    )
    email_service.sent_messages.clear()
    await client.post(
        "/api/auth/change-email",
        json={
            "new_email": "alice-new@example.com",
            "current_password": "password123",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    token = _change_email_token_from_outbox(
        email_service, to="alice-new@example.com"
    )

    async with db_mod.SessionLocal() as session:
        row = (
            await session.execute(
                select(EmailVerification).where(EmailVerification.token == token)
            )
        ).scalar_one()
        row.expires_at = "2000-01-01T00:00:00Z"
        await session.commit()

    r = await client.get("/api/auth/verify-new-email", params={"token": token})
    assert r.status_code == 410, r.text


@pytest.mark.asyncio
async def test_change_email_same_as_current_returns_400(client, email_service):
    jwt = await _register_and_login(
        client,
        username="alice",
        email="alice@example.com",
        password="password123",
    )
    email_service.sent_messages.clear()

    r = await client.post(
        "/api/auth/change-email",
        json={
            "new_email": "alice@example.com",
            "current_password": "password123",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 400, r.text
    # Casing must not bypass the same-email check.
    r2 = await client.post(
        "/api/auth/change-email",
        json={
            "new_email": "ALICE@example.com",
            "current_password": "password123",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r2.status_code == 400, r2.text
