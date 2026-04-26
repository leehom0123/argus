"""Tests for /api/notifications CRUD + ack + mark-all + access control."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_and_login(
    client: AsyncClient, username: str, email: str, password: str = "password123"
) -> str:
    """Register a new user and return its JWT."""
    reg = await client.post(
        "/api/auth/register",
        json={"username": username, "email": email, "password": password},
        headers={},  # no auth header for registration
    )
    assert reg.status_code == 201, reg.text

    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": password},
        headers={},
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def _insert_notification(
    client: AsyncClient,
    jwt: str,
    *,
    rule_id: str = "val_loss_diverging",
    severity: str = "warn",
    title: str = "Test alert",
    body: str = "Something diverged.",
) -> dict:
    """Insert a notification directly via the watchdog internals through DB.

    We use the internal DB session here rather than a fabricated API
    because there is no public POST /api/notifications endpoint —
    notifications are created by the watchdog only.

    Returns the serialised row dict as fetched via GET.
    """
    # Reach into the app DB via the test client's app state.
    from backend.db import SessionLocal
    from backend.models import Notification
    from datetime import datetime, timezone

    now = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    # Resolve the user_id from the JWT.
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert me.status_code == 200
    user_id = me.json()["id"]

    async with SessionLocal() as db:
        row = Notification(
            user_id=user_id,
            batch_id="batch-xyz",
            rule_id=rule_id,
            severity=severity,
            title=title,
            body=body,
            created_at=now,
            read_at=None,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {"id": row.id, "user_id": row.user_id}


# ---------------------------------------------------------------------------
# List endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_notifications_empty(client: AsyncClient) -> None:
    """Fresh user has no notifications."""
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    resp = await client.get(
        "/api/notifications", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_notifications_returns_own_rows(client: AsyncClient) -> None:
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    info = await _insert_notification(client, jwt, title="My alert")
    resp = await client.get(
        "/api/notifications", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["title"] == "My alert"
    assert rows[0]["read_at"] is None


@pytest.mark.asyncio
async def test_list_unread_only_filter(client: AsyncClient) -> None:
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    info = await _insert_notification(client, jwt)
    nid = info["id"]

    # Ack it
    await client.post(
        f"/api/notifications/{nid}/ack",
        headers={"Authorization": f"Bearer {jwt}"},
    )

    # Insert a second unread one
    await _insert_notification(client, jwt, title="Second alert")

    resp = await client.get(
        "/api/notifications?unread_only=true",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["title"] == "Second alert"


# ---------------------------------------------------------------------------
# Ack endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_marks_read(client: AsyncClient) -> None:
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    info = await _insert_notification(client, jwt)
    nid = info["id"]

    resp = await client.post(
        f"/api/notifications/{nid}/ack",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert resp.status_code == 204

    # Verify read_at is now set
    list_resp = await client.get(
        "/api/notifications", headers={"Authorization": f"Bearer {jwt}"}
    )
    rows = list_resp.json()
    assert rows[0]["read_at"] is not None


@pytest.mark.asyncio
async def test_ack_idempotent(client: AsyncClient) -> None:
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    info = await _insert_notification(client, jwt)
    nid = info["id"]

    headers = {"Authorization": f"Bearer {jwt}"}
    assert (await client.post(f"/api/notifications/{nid}/ack", headers=headers)).status_code == 204
    assert (await client.post(f"/api/notifications/{nid}/ack", headers=headers)).status_code == 204


# ---------------------------------------------------------------------------
# Mark all read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_all_read(client: AsyncClient) -> None:
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await _insert_notification(client, jwt, title="A")
    await _insert_notification(client, jwt, title="B")
    await _insert_notification(client, jwt, title="C")

    resp = await client.post(
        "/api/notifications/mark_all_read",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert resp.status_code == 204

    list_resp = await client.get(
        "/api/notifications", headers={"Authorization": f"Bearer {jwt}"}
    )
    for row in list_resp.json():
        assert row["read_at"] is not None


# ---------------------------------------------------------------------------
# Delete endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_notification(client: AsyncClient) -> None:
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    info = await _insert_notification(client, jwt)
    nid = info["id"]

    resp = await client.delete(
        f"/api/notifications/{nid}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert resp.status_code == 204

    list_resp = await client.get(
        "/api/notifications", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert list_resp.json() == []


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_404(client: AsyncClient) -> None:
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    resp = await client.delete(
        "/api/notifications/999999",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Access control — user cannot see / mutate another user's notifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_control_cannot_ack_other_users_notification(
    client: AsyncClient,
) -> None:
    """User A's notification should not be accessible by user B."""
    # tester (default user) inserts a notification
    jwt_a = client._test_default_jwt  # type: ignore[attr-defined]
    info = await _insert_notification(client, jwt_a, title="Private alert")
    nid = info["id"]

    # Register user B and try to ack A's notification
    jwt_b = await _register_and_login(
        client, "other_user", "other@example.com"
    )
    resp = await client.post(
        f"/api/notifications/{nid}/ack",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_access_control_cannot_delete_other_users_notification(
    client: AsyncClient,
) -> None:
    jwt_a = client._test_default_jwt  # type: ignore[attr-defined]
    info = await _insert_notification(client, jwt_a)
    nid = info["id"]

    jwt_b = await _register_and_login(
        client, "other_user2", "other2@example.com"
    )
    resp = await client.delete(
        f"/api/notifications/{nid}",
        headers={"Authorization": f"Bearer {jwt_b}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_only_returns_own_rows(client: AsyncClient) -> None:
    """User B's list must not include user A's notifications."""
    jwt_a = client._test_default_jwt  # type: ignore[attr-defined]
    await _insert_notification(client, jwt_a, title="A's private alert")

    jwt_b = await _register_and_login(
        client, "list_test_user", "listtest@example.com"
    )
    resp = await client.get(
        "/api/notifications", headers={"Authorization": f"Bearer {jwt_b}"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requires_auth(unauthed_client: AsyncClient) -> None:
    """No auth header → 401."""
    resp = await unauthed_client.get("/api/notifications")
    assert resp.status_code == 401
