"""Tests for POST /api/admin/email/smtp/test (Team Email / BE-2)."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_smtp_test_requires_admin(client):
    r = await client.post(
        "/api/auth/register",
        json={"username": "normie", "email": "normie@example.com", "password": "password123"},
    )
    assert r.status_code == 201
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "normie", "password": "password123"},
    )
    jwt = login.json()["access_token"]
    r = await client.post(
        "/api/admin/email/smtp/test",
        json={"host": "smtp.example.com", "port": 587, "username": "u",
              "password": "hunter2", "use_tls": True,
              "from_addr": "noreply@example.com"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_smtp_test_success(client, monkeypatch):
    from backend.api import email_admin

    async def fake(**kwargs):
        return True, "sent"

    monkeypatch.setattr(email_admin, "_smtp_test_impl", fake)
    r = await client.post(
        "/api/admin/email/smtp/test",
        json={"host": "smtp.example.com", "port": 587, "username": "u",
              "password": "hunter2", "use_tls": True,
              "from_addr": "noreply@example.com"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["message"] == "sent"


@pytest.mark.asyncio
async def test_smtp_test_failure_sanitises_error(client, monkeypatch):
    from backend.api import email_admin

    async def fake(**kwargs):
        return False, "SMTPAuthenticationError: authentication failed"

    monkeypatch.setattr(email_admin, "_smtp_test_impl", fake)
    r = await client.post(
        "/api/admin/email/smtp/test",
        json={"host": "smtp.example.com", "port": 587, "username": "u",
              "password": "hunter2-secret", "use_tls": True,
              "from_addr": "noreply@example.com"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "hunter2-secret" not in r.text


@pytest.mark.asyncio
async def test_stats_endpoint(client):
    r = await client.get("/api/admin/email/stats")
    assert r.status_code == 200
    body = r.json()
    for k in ("queued", "sent_last_hour", "failed_last_hour", "deadletter_count"):
        assert k in body


@pytest.mark.asyncio
async def test_dead_letter_list_schema_unavailable(client):
    """Pre-schema the endpoint must soft-fail, not 500."""
    r = await client.get("/api/admin/email/dead-letter?status=pending")
    assert r.status_code == 200
    assert "items" in r.json()
