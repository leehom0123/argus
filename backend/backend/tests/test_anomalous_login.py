"""Tests for the anomalous-login detector (Team A / roadmap #33).

The detector lives in ``backend.api.auth._check_anomalous_login`` and
fires an email via ``EmailService.send_anomalous_login`` when a login
succeeds from a ``(ip, user_agent)`` pair not seen in the past 30
days.  First-ever login after registration must NOT fire (the user
has no prior history to compare against).
"""
from __future__ import annotations

import asyncio
import json

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
    assert r.status_code == 201, r.text


async def _login(client, *, user_agent: str, username: str = "alice"):
    # Strip any auth header from the pre-authed ``client`` fixture so the
    # login endpoint re-runs the full detector instead of resolving via
    # the existing API token.
    client.headers.pop("Authorization", None)
    r = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
        headers={"User-Agent": user_agent},
    )
    return r


@pytest.fixture(autouse=True)
def _enable_anomalous_login(monkeypatch):
    """Turn on the feature for this module only (default conftest is off)."""
    from backend.config import get_settings

    monkeypatch.setenv("ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_first_login_is_silent(client, email_service):
    await _register(client)
    r = await _login(client, user_agent="Mozilla/5.0 test-ua-first")
    assert r.status_code == 200

    # Flush pending background tasks (the email fires via create_task).
    await asyncio.sleep(0.05)

    assert all(
        "<anomalous_login-inline>" != m.template
        for m in email_service.sent_messages
    ), "first-ever login must not trigger the anomalous-login email"


@pytest.mark.asyncio
async def test_second_login_same_ua_is_silent(client, email_service):
    await _register(client)
    # 1st login registers the (ip, UA) pair.
    r1 = await _login(client, user_agent="test-ua-returning")
    assert r1.status_code == 200
    # 2nd login from the SAME UA — must NOT re-alert.
    r2 = await _login(client, user_agent="test-ua-returning")
    assert r2.status_code == 200

    await asyncio.sleep(0.05)
    alerts = [
        m for m in email_service.sent_messages
        if m.template == "<anomalous_login-inline>"
    ]
    assert alerts == []


@pytest.mark.asyncio
async def test_login_from_new_ua_triggers_email(client, email_service):
    await _register(client)
    # 1st login seeds the known list.
    r1 = await _login(client, user_agent="test-ua-home-laptop")
    assert r1.status_code == 200
    # 2nd login from a DIFFERENT UA → should fire.
    r2 = await _login(client, user_agent="test-ua-airport-kiosk")
    assert r2.status_code == 200

    await asyncio.sleep(0.1)
    alerts = [
        m for m in email_service.sent_messages
        if m.template == "<anomalous_login-inline>"
    ]
    assert len(alerts) == 1
    msg = alerts[0]
    assert msg.to == "alice@example.com"
    assert "New sign-in" in msg.subject or "新登录" in msg.subject
    assert "test-ua-airport-kiosk" in msg.body_html


@pytest.mark.asyncio
async def test_known_ips_json_is_persisted(client):
    """After a login the user's ``known_ips_json`` column stores the pair."""
    await _register(client)
    r = await _login(client, user_agent="Mozilla/5.0 persistence-check")
    assert r.status_code == 200

    from backend.db import SessionLocal
    from backend.models import User
    from sqlalchemy import select

    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        assert user.known_ips_json is not None
        entries = json.loads(user.known_ips_json)
        assert isinstance(entries, list)
        assert len(entries) == 1
        assert "ua_hash" in entries[0]
        assert "last_seen" in entries[0]


@pytest.mark.asyncio
async def test_disabled_flag_suppresses_alert(client, email_service, monkeypatch):
    """With the feature flag off, even a brand-new UA produces no email."""
    from backend.config import get_settings

    monkeypatch.setenv("ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED", "false")
    get_settings.cache_clear()

    await _register(client)
    r1 = await _login(client, user_agent="first-ua")
    r2 = await _login(client, user_agent="second-ua-should-not-alert")
    assert r1.status_code == 200
    assert r2.status_code == 200

    await asyncio.sleep(0.05)
    alerts = [
        m for m in email_service.sent_messages
        if m.template == "<anomalous_login-inline>"
    ]
    assert alerts == []
