"""Audit log: writes land on register / token_create, pagination works."""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_register_writes_audit_row(client):
    """Registering a new user leaves a ``register`` row in audit_log."""
    # Tester is already registered by conftest — register a second user.
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        },
    )
    assert r.status_code == 201

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.get(
        "/api/admin/audit-log?action=register",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200
    rows = r.json()
    # Both tester + alice registered.
    actions = [r["action"] for r in rows]
    assert actions.count("register") >= 2
    # Metadata decoded to a dict
    for row in rows:
        assert row["action"] == "register"
        if row["metadata"] is not None:
            assert "username" in row["metadata"]


@pytest.mark.asyncio
async def test_token_create_writes_audit_row(client):
    """POST /api/tokens enters a ``token_create`` audit row."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/tokens",
        json={"name": "audit-me", "scope": "reporter"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201

    # Give the background write a beat.
    for _ in range(10):
        r = await client.get(
            "/api/admin/audit-log?action=token_create",
            headers={"Authorization": f"Bearer {tester_jwt}"},
        )
        if r.status_code == 200 and any(
            (row.get("metadata") or {}).get("name") == "audit-me"
            for row in r.json()
        ):
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError("token_create audit row never materialised")


@pytest.mark.asyncio
async def test_audit_log_pagination(client):
    """Limit + offset pagination returns well-ordered rows."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    headers = {"Authorization": f"Bearer {tester_jwt}"}

    # Trigger a few audit rows by registering extra users.
    for i in range(5):
        await client.post(
            "/api/auth/register",
            json={
                "username": f"u{i}",
                "email": f"u{i}@example.com",
                "password": "password123",
            },
        )

    # First page
    r = await client.get(
        "/api/admin/audit-log?limit=2&offset=0", headers=headers
    )
    assert r.status_code == 200
    page1 = r.json()
    assert len(page1) == 2

    # Next page doesn't repeat the same rows.
    r = await client.get(
        "/api/admin/audit-log?limit=2&offset=2", headers=headers
    )
    page2 = r.json()
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})

    # Timestamps strictly non-increasing (newest-first)
    ts = [r["timestamp"] for r in page1 + page2]
    assert ts == sorted(ts, reverse=True)
