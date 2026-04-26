"""Tests for the /api/tokens endpoints.

Covers the full token lifecycle:
  * create → returns plaintext once, 201
  * list → hides revoked by default, no plaintext
  * revoke → idempotent soft-delete
  * expires_at in the past → token fails auth
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select


async def _jwt_for_tester(client) -> str:
    """Grab the pre-registered default JWT out of the conftest client."""
    return getattr(client, "_test_default_jwt")


@pytest.mark.asyncio
async def test_create_token_returns_plaintext_once(client):
    jwt = await _jwt_for_tester(client)
    r = await client.post(
        "/api/tokens",
        json={"name": "laptop-reporter", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Plaintext is present and prefix-tagged.
    assert body["token"].startswith("em_live_")
    assert len(body["token"]) > 20
    assert body["prefix"] == "em_live_"
    # display_hint is the first 8 chars.
    assert body["display_hint"] == body["token"][:8]
    assert body["scope"] == "reporter"
    assert body["revoked"] is False

    # A subsequent list must NOT expose the plaintext token field.
    lr = await client.get(
        "/api/tokens", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert lr.status_code == 200
    items = lr.json()
    assert all("token" not in it for it in items)
    # At minimum our default token + the one we just created are listed.
    assert len(items) >= 2


@pytest.mark.asyncio
async def test_list_tokens_returns_only_my_own(client):
    """A second user's tokens must not leak into the first user's list."""
    jwt = await _jwt_for_tester(client)
    # Register a second user and mint a token as them.
    other_reg = await client.post(
        "/api/auth/register",
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": "password123",
        },
    )
    assert other_reg.status_code == 201
    other_login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "bob", "password": "password123"},
    )
    bob_jwt = other_login.json()["access_token"]
    bob_tok = await client.post(
        "/api/tokens",
        json={"name": "bob-laptop", "scope": "viewer"},
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert bob_tok.status_code == 201

    my_list = await client.get(
        "/api/tokens", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert my_list.status_code == 200
    names = [it["name"] for it in my_list.json()]
    assert "bob-laptop" not in names


@pytest.mark.asyncio
async def test_revoke_is_idempotent(client):
    jwt = await _jwt_for_tester(client)
    create = await client.post(
        "/api/tokens",
        json={"name": "soon-gone", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    token_id = create.json()["id"]

    # First revoke: 200 + detail=revoked
    r1 = await client.delete(
        f"/api/tokens/{token_id}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r1.status_code == 200
    assert r1.json()["detail"] == "revoked"

    # Second revoke: still 200, detail=already-revoked
    r2 = await client.delete(
        f"/api/tokens/{token_id}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r2.status_code == 200
    assert r2.json()["detail"] == "already-revoked"

    # By default list omits revoked tokens…
    lst = await client.get(
        "/api/tokens", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert not any(it["id"] == token_id for it in lst.json())

    # …but include_revoked=true surfaces them.
    lst_all = await client.get(
        "/api/tokens?include_revoked=true",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert any(
        it["id"] == token_id and it["revoked"] is True
        for it in lst_all.json()
    )


@pytest.mark.asyncio
async def test_cannot_revoke_another_users_token(client):
    """Revoking a token you don't own must 404, not 403.

    We use 404 instead of 403 to avoid leaking which token IDs exist.
    """
    jwt = await _jwt_for_tester(client)
    # Second user creates a token.
    await client.post(
        "/api/auth/register",
        json={
            "username": "eve",
            "email": "eve@example.com",
            "password": "password123",
        },
    )
    eve_login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "eve", "password": "password123"},
    )
    eve_jwt = eve_login.json()["access_token"]
    eve_tok = await client.post(
        "/api/tokens",
        json={"name": "eve-reporter", "scope": "reporter"},
        headers={"Authorization": f"Bearer {eve_jwt}"},
    )
    eve_token_id = eve_tok.json()["id"]

    # tester (admin, but we don't grant cross-user mgmt) tries to revoke eve's.
    r = await client.delete(
        f"/api/tokens/{eve_token_id}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_expired_token_fails_auth_on_events(client):
    """A token with expires_at in the past returns 401 on use."""
    import backend.db as db_mod
    from backend.models import ApiToken

    jwt = await _jwt_for_tester(client)
    cr = await client.post(
        "/api/tokens",
        json={"name": "short-lived", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    plaintext = cr.json()["token"]
    token_id = cr.json()["id"]

    # Manually backdate expires_at to before now.
    past = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with db_mod.SessionLocal() as session:
        row = (
            await session.execute(
                select(ApiToken).where(ApiToken.id == token_id)
            )
        ).scalar_one()
        row.expires_at = past
        await session.commit()

    # Try to use that token to POST an event.
    ev = {
        "event_id": "00000000-0000-4000-8000-expire000001",
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b-expire",
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
    }
    r = await client.post(
        "/api/events",
        json=ev,
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_token_requires_jwt_not_api_token(client):
    """em_live_ tokens cannot mint new tokens (no privilege escalation)."""
    # The default client has an em_live_ token as Authorization.
    r = await client.post(
        "/api/tokens",
        json={"name": "hacked", "scope": "reporter"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_list_tokens_scope_filter(client):
    jwt = await _jwt_for_tester(client)
    # Mint one of each scope.
    await client.post(
        "/api/tokens",
        json={"name": "my-reporter", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    await client.post(
        "/api/tokens",
        json={"name": "my-viewer", "scope": "viewer"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    only_viewer = await client.get(
        "/api/tokens?scope=viewer",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    names = [it["name"] for it in only_viewer.json()]
    assert "my-viewer" in names
    assert "my-reporter" not in names

    bad = await client.get(
        "/api/tokens?scope=bogus",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert bad.status_code == 400
