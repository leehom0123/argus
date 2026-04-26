"""Password reset flow tests."""
from __future__ import annotations

import pytest
from sqlalchemy import select


async def _register(client):
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        },
    )
    assert r.status_code == 201


def _reset_token_from_outbox(email_service) -> str:
    reset = [
        m for m in email_service.sent_messages if m.template == "reset_password.en-US.html"
    ]
    assert reset, "expected a reset_password email in the outbox"
    return reset[-1].context["reset_url"].split("token=")[-1]


@pytest.mark.asyncio
async def test_request_reset_sends_email(client, email_service):
    await _register(client)
    email_service.sent_messages.clear()

    r = await client.post(
        "/api/auth/request-password-reset",
        json={"email": "alice@example.com"},
    )
    assert r.status_code == 200

    sent = [
        m for m in email_service.sent_messages if m.template == "reset_password.en-US.html"
    ]
    assert len(sent) == 1
    assert sent[0].to == "alice@example.com"
    assert "token=" in sent[0].context["reset_url"]


@pytest.mark.asyncio
async def test_unknown_email_silently_returns_200(client, email_service):
    email_service.sent_messages.clear()
    r = await client.post(
        "/api/auth/request-password-reset",
        json={"email": "ghost@example.com"},
    )
    # Must not leak whether the email is registered.
    assert r.status_code == 200
    assert not any(
        m.template == "reset_password.en-US.html"
        for m in email_service.sent_messages
    )


@pytest.mark.asyncio
async def test_reset_consumes_token_and_changes_password(client, email_service):
    await _register(client)
    email_service.sent_messages.clear()
    await client.post(
        "/api/auth/request-password-reset",
        json={"email": "alice@example.com"},
    )
    token = _reset_token_from_outbox(email_service)

    r = await client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "newpass9876"},
    )
    assert r.status_code == 200, r.text

    # Old password no longer works.
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

    # Token is now single-use.
    r2 = await client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "anotherpass55"},
    )
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_reset_expired_token(client, email_service):
    import backend.db as db_mod
    from backend.models import EmailVerification

    await _register(client)
    email_service.sent_messages.clear()
    await client.post(
        "/api/auth/request-password-reset",
        json={"email": "alice@example.com"},
    )
    token = _reset_token_from_outbox(email_service)

    async with db_mod.SessionLocal() as session:
        row = (
            await session.execute(
                select(EmailVerification).where(EmailVerification.token == token)
            )
        ).scalar_one()
        row.expires_at = "2000-01-01T00:00:00Z"
        await session.commit()

    r = await client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "newpass9876"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_reset_clears_lockout(client, email_service):
    """Completing a password reset should clear any active lockout state."""
    import backend.db as db_mod
    from backend.models import User

    await _register(client)
    # Trigger 5 failed logins to arm the lockout.
    for _ in range(5):
        await client.post(
            "/api/auth/login",
            json={"username_or_email": "alice", "password": "wrongpass123"},
        )

    email_service.sent_messages.clear()
    await client.post(
        "/api/auth/request-password-reset",
        json={"email": "alice@example.com"},
    )
    token = _reset_token_from_outbox(email_service)
    await client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "newpass9876"},
    )

    async with db_mod.SessionLocal() as session:
        u = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        assert u.failed_login_count == 0
        assert u.locked_until is None
