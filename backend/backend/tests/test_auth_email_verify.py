"""Email verification tests."""
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


async def _token_from_outbox(email_service) -> str:
    assert email_service.sent_messages, "no verification email was sent"
    return email_service.sent_messages[-1].context["verify_url"].split("token=")[-1]


@pytest.mark.asyncio
async def test_verify_happy_path(client, email_service):
    await _register(client)
    token = await _token_from_outbox(email_service)

    r = await client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # Log in and confirm email_verified=true now.
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    assert lr.status_code == 200
    me = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {lr.json()['access_token']}"},
    )
    assert me.json()["email_verified"] is True


@pytest.mark.asyncio
async def test_verify_token_is_one_shot(client, email_service):
    await _register(client)
    token = await _token_from_outbox(email_service)

    r1 = await client.post("/api/auth/verify-email", json={"token": token})
    assert r1.status_code == 200

    r2 = await client.post("/api/auth/verify-email", json={"token": token})
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_verify_expired_token(client, email_service):
    """Manually push expires_at into the past — same effect as a 24h wait."""
    import backend.db as db_mod
    from backend.models import EmailVerification

    await _register(client)
    token = await _token_from_outbox(email_service)

    async with db_mod.SessionLocal() as session:
        row = (
            await session.execute(
                select(EmailVerification).where(EmailVerification.token == token)
            )
        ).scalar_one()
        row.expires_at = "2000-01-01T00:00:00Z"
        await session.commit()

    r = await client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_verify_unknown_token(client):
    r = await client.post(
        "/api/auth/verify-email", json={"token": "totally-made-up"}
    )
    assert r.status_code == 400
