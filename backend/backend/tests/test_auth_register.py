"""Registration endpoint tests."""
from __future__ import annotations

import pytest


_GOOD = {
    "username": "alice",
    "email": "alice@example.com",
    "password": "password123",
}


@pytest.mark.asyncio
async def test_register_first_user_is_admin(unauthed_client, email_service):
    """First registered user should be promoted to admin automatically.

    Note: this test uses ``unauthed_client`` rather than the default
    ``client`` because the latter auto-registers ``tester`` for API-
    token setup; that would claim the "first user" slot and make alice
    show up as non-admin.
    """
    client = unauthed_client
    r = await client.post("/api/auth/register", json=_GOOD)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["require_verify"] is True
    assert body["user_id"] >= 1

    # Verification email was rendered + captured by the dev-stdout fallback.
    assert len(email_service.sent_messages) == 1
    sent = email_service.sent_messages[0]
    assert sent.to == "alice@example.com"
    assert sent.template in ("verify.en-US.html", "verify.zh-CN.html"), (
        f"Unexpected template: {sent.template!r}"
    )
    assert "verify" in sent.body_html.lower()
    # URL must contain the token and point at the configured base URL.
    token = sent.context["verify_url"].split("token=")[-1]
    assert len(token) > 20
    assert sent.context["verify_url"].startswith("http://localhost:5173")

    # /auth/login should succeed even pre-verification (per requirements
    # choice: allow unverified login, block only sensitive operations).
    r2 = await client.post(
        "/api/auth/login",
        json={
            "username_or_email": "alice",
            "password": "password123",
        },
    )
    assert r2.status_code == 200, r2.text
    token = r2.json()["access_token"]

    me = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me.status_code == 200
    assert me.json()["is_admin"] is True
    assert me.json()["email_verified"] is False


@pytest.mark.asyncio
async def test_register_second_user_not_admin(client):
    await client.post("/api/auth/register", json=_GOOD)
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": "password123",
        },
    )
    assert r.status_code == 201

    # Log bob in and check non-admin.
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": "bob", "password": "password123"},
    )
    token = lr.json()["access_token"]
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.json()["is_admin"] is False


@pytest.mark.asyncio
async def test_register_duplicate_username(client):
    r1 = await client.post("/api/auth/register", json=_GOOD)
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "email": "other@example.com",
            "password": "password123",
        },
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    await client.post("/api/auth/register", json=_GOOD)
    r2 = await client.post(
        "/api/auth/register",
        json={
            "username": "alice2",
            "email": "ALICE@example.com",  # case-insensitive match
            "password": "password123",
        },
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_register_invalid_email(client):
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "carol",
            "email": "not-an-email",
            "password": "password123",
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_weak_password_too_short(client):
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "carol",
            "email": "carol@example.com",
            "password": "short1",
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_weak_password_no_digit(client):
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "carol",
            "email": "carol@example.com",
            "password": "onlyletters",
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_invalid_username(client):
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "ab",  # too short
            "email": "ab@example.com",
            "password": "password123",
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_smtp_failure_still_returns_201(client, monkeypatch):
    """Email service failure must not block registration response."""
    from backend.services import email as email_mod

    async def _boom(self, **kwargs):
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(email_mod.EmailService, "send_verification", _boom)
    r = await client.post("/api/auth/register", json=_GOOD)
    assert r.status_code == 201, r.text
