"""Auth-dependency tests for API tokens in :mod:`backend.deps`.

The target is :func:`backend.deps.get_current_user`: it must:

* accept valid em_live_ / em_view_ tokens
* reject unknown tokens with 401 (+ WWW-Authenticate header)
* reject revoked tokens
* reject expired tokens
* NOT leak last_used updates as a synchronous failure
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select


async def _make_token(client, scope: str) -> str:
    """Mint a fresh token of the given scope via the default JWT."""
    jwt = getattr(client, "_test_default_jwt")
    r = await client.post(
        "/api/tokens",
        json={"name": f"test-{scope}", "scope": scope},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["token"]


@pytest.mark.asyncio
async def test_em_live_token_authenticates_on_me(client):
    token = await _make_token(client, "reporter")
    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    # Owner identity is preserved — "me" reports the token's owning user.
    assert r.json()["username"] == "tester"


@pytest.mark.asyncio
async def test_em_view_token_authenticates_on_me(client):
    token = await _make_token(client, "viewer")
    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_invalid_em_live_token_401(unauthed_client):
    r = await unauthed_client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer em_live_this_does_not_exist"},
    )
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")


@pytest.mark.asyncio
async def test_revoked_token_is_401(client):
    jwt = getattr(client, "_test_default_jwt")
    cr = await client.post(
        "/api/tokens",
        json={"name": "revoke-me", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    plaintext = cr.json()["token"]
    token_id = cr.json()["id"]

    # Token works now.
    r1 = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert r1.status_code == 200

    # Revoke it.
    rv = await client.delete(
        f"/api/tokens/{token_id}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert rv.status_code == 200

    # Same plaintext no longer authenticates.
    r2 = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_is_401(client):
    """expires_at in the past → 401 regardless of revoked flag."""
    import backend.db as db_mod
    from backend.models import ApiToken

    jwt = getattr(client, "_test_default_jwt")
    cr = await client.post(
        "/api/tokens",
        json={"name": "will-expire", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    plaintext = cr.json()["token"]
    token_id = cr.json()["id"]

    past = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with db_mod.SessionLocal() as session:
        row = (
            await session.execute(
                select(ApiToken).where(ApiToken.id == token_id)
            )
        ).scalar_one()
        row.expires_at = past
        await session.commit()

    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_last_used_gets_bumped(client):
    """last_used is set asynchronously after an authenticated call.

    We can't assert on the exact timestamp (the bump runs in a
    fire-and-forget task) but we can poll briefly and confirm it moves
    from NULL → non-NULL.
    """
    import backend.db as db_mod
    from backend.models import ApiToken

    jwt = getattr(client, "_test_default_jwt")
    cr = await client.post(
        "/api/tokens",
        json={"name": "bumpy", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    plaintext = cr.json()["token"]
    token_id = cr.json()["id"]

    # Before use, last_used is NULL.
    async with db_mod.SessionLocal() as session:
        row = (
            await session.execute(
                select(ApiToken).where(ApiToken.id == token_id)
            )
        ).scalar_one()
        assert row.last_used is None

    # Use the token.
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert me.status_code == 200

    # Poll up to ~1s for the background bump to land.
    last_used = None
    for _ in range(20):
        async with db_mod.SessionLocal() as session:
            row = (
                await session.execute(
                    select(ApiToken).where(ApiToken.id == token_id)
                )
            ).scalar_one()
            last_used = row.last_used
            if last_used is not None:
                break
        await asyncio.sleep(0.05)
    assert last_used is not None, "last_used should be populated after use"
