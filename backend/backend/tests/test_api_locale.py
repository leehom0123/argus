"""Regression tests verifying that HTTPException detail strings are localised.

Three scenarios per the task spec:
1. POST /api/auth/login wrong password + Accept-Language: zh-CN → Chinese detail.
2. Same endpoint without Accept-Language → English detail.
3. GET /api/batches/<nonexistent> + Accept-Language: zh-CN → "批次不存在"-family Chinese.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_login_wrong_password_zh_cn(client):
    """Wrong password + zh-CN header → Chinese error text in detail."""
    # Register a second user so we have a known account to try bad creds on.
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "alice_locale",
            "email": "alice_locale@example.com",
            "password": "correctpass123",
        },
    )
    assert r.status_code == 201

    resp = await client.post(
        "/api/auth/login",
        json={
            "username_or_email": "alice_locale",
            "password": "wrongpassword",
        },
        headers={"Accept-Language": "zh-CN, en-US;q=0.8"},
    )
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    # The zh-CN translation for auth.credentials.bad
    assert "邮箱" in detail or "密码" in detail, f"Expected Chinese, got: {detail!r}"


@pytest.mark.asyncio
async def test_login_wrong_password_en_us(client):
    """Wrong password without Accept-Language → English error text."""
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "bob_locale",
            "email": "bob_locale@example.com",
            "password": "correctpass123",
        },
    )
    assert r.status_code == 201

    resp = await client.post(
        "/api/auth/login",
        json={
            "username_or_email": "bob_locale",
            "password": "wrongpassword",
        },
        # No Accept-Language header → defaults to en-US
    )
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert "Invalid" in detail or "password" in detail.lower(), (
        f"Expected English, got: {detail!r}"
    )


@pytest.mark.asyncio
async def test_get_nonexistent_batch_zh_cn(client):
    """GET /api/batches/<nonexistent> + zh-CN → Chinese 'batch not found' message."""
    resp = await client.get(
        "/api/batches/nonexistent-batch-id-xyz",
        headers={"Accept-Language": "zh-CN"},
    )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    # The zh-CN translation for batch.not_found is "未找到该批次"
    assert "批次" in detail or "未找到" in detail, (
        f"Expected Chinese batch-not-found, got: {detail!r}"
    )
