"""End-to-end tests for the dual-key JWT rotation flow (v0.2 #109).

Eight scenarios cover the spec:

1. ``GET /api/admin/security/jwt/status`` reports the empty initial
   state and is admin-gated.
2. ``POST /api/admin/security/jwt/rotate`` mints a new secret and
   writes both ``current_secret`` + ``rotated_at``.
3. After a rotation, a token issued **before** the rotation still
   verifies (the previous secret is honoured during the grace).
4. After a rotation, freshly-issued tokens use the new secret AND
   they verify on subsequent requests.
5. Once the previous secret is past its 24h grace, the verifier
   stops accepting it (we monkey-patch the rotated_at timestamp to
   be old enough that ``clear_expired_previous`` wipes the row).
6. Non-admin callers get 403 on both endpoints.
7. A second rotation inside the 60s cooldown window is rejected with
   429 + ``Retry-After`` (anti-double-rotate footgun guard).
8. Once the cooldown elapses (simulated by aging ``rotated_at``), a
   follow-up rotation succeeds.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend import models  # noqa: F401  - register tables for the in-memory DB
from backend.db import SessionLocal
from backend.services import jwt_rotation
from backend.services.runtime_config import set_config


async def _login_admin(client) -> str:
    """Return a fresh JWT for the default test ``tester`` (admin) user.

    The conftest ``client`` fixture defaults to using an API token; for
    the admin endpoints we want a JWT so the rotation cache path is
    exercised end-to-end. We pull the cached JWT off the client.
    """
    return getattr(client, "_test_default_jwt")


@pytest.mark.asyncio
async def test_jwt_status_initial_state(client) -> None:
    """Fresh deploy: never rotated, no previous, no expiry."""
    jwt_token = await _login_admin(client)
    r = await client.get(
        "/api/admin/security/jwt/status",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rotated_at"] is None
    assert body["has_previous"] is False
    assert body["previous_expires_at"] is None
    # 24h grace echoed back so the UI can label the countdown.
    assert body["grace_seconds"] == 24 * 60 * 60


@pytest.mark.asyncio
async def test_jwt_rotate_mints_new_secret_and_writes_rotated_at(client) -> None:
    """A successful rotation persists current + rotated_at."""
    jwt_token = await _login_admin(client)
    r = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rotated_at"]  # ISO timestamp string
    assert body["grace_seconds"] == 24 * 60 * 60

    # Verify the DB-level state matches the cache via load_secrets.
    async with SessionLocal() as db:
        current, _previous, rotated_at = await jwt_rotation.load_secrets(db)
    assert current  # non-empty
    assert rotated_at == body["rotated_at"]


@pytest.mark.asyncio
async def test_token_issued_before_rotation_still_verifies(client) -> None:
    """Already-issued tokens survive a rotation thanks to the dual-key path."""
    # Step 1: log in, snag a JWT signed with the env-fallback secret.
    jwt_token = await _login_admin(client)
    me_before = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert me_before.status_code == 200

    # Step 2: rotate. The rotate endpoint itself uses the same JWT,
    # which validates against the env secret first.
    rot = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert rot.status_code == 200, rot.text

    # Step 3: re-use the OLD token. It must still authenticate because
    # the env secret is in the candidate list (legacy fallback) and the
    # ``previous_secret`` (empty pre-first-rotation) is harmless.
    me_after = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert me_after.status_code == 200, me_after.text


@pytest.mark.asyncio
async def test_freshly_issued_token_after_rotation_verifies(client) -> None:
    """Tokens minted post-rotation use the new secret and verify cleanly."""
    jwt_token = await _login_admin(client)
    rot = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert rot.status_code == 200

    # Log in again — this issues a new token signed with current_secret.
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "tester", "password": "password123"},
    )
    assert login.status_code == 200, login.text
    new_jwt = login.json()["access_token"]
    assert new_jwt != jwt_token

    me = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {new_jwt}"},
    )
    assert me.status_code == 200, me.text


@pytest.mark.asyncio
async def test_previous_secret_cleared_after_grace_window(client) -> None:
    """Once 24h elapses, ``previous_secret`` is wiped by the sweeper."""
    jwt_token = await _login_admin(client)
    # First rotation establishes current + rotated_at; previous stays empty
    # (no prior secret existed). Do a SECOND rotation so we get a real
    # previous_secret to test the grace window against.
    r1 = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert r1.status_code == 200
    # Age rotated_at past the 60s anti-double-rotate cooldown so the
    # second rotation isn't 429'd. (See test_rotate_rejected_within_60s_cooldown
    # for the cooldown coverage.)
    aged = (
        datetime.now(timezone.utc) - timedelta(seconds=jwt_rotation.ROTATE_COOLDOWN_SECONDS + 5)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with SessionLocal() as _db:
        await set_config(
            _db, group="jwt", key="rotated_at",
            value=aged, encrypted=False,
        )
        await _db.commit()
    jwt_rotation.reset_cache_for_tests()
    # Re-login with the new secret so the next rotation call has a
    # token signed by the live current.
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "tester", "password": "password123"},
    )
    fresh_jwt = login.json()["access_token"]
    r2 = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {fresh_jwt}"},
    )
    assert r2.status_code == 200

    # Confirm previous is now non-empty.
    async with SessionLocal() as db:
        _c, previous, rotated_at = await jwt_rotation.load_secrets(db)
        assert previous, "expected previous_secret to be populated after second rotation"

        # Force-age the rotated_at marker so the sweeper considers the
        # grace window expired. Bypassing time.sleep keeps the test
        # deterministic in CI.
        old_ts = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        await set_config(
            db, group="jwt", key="rotated_at",
            value=old_ts, encrypted=False,
        )
        await db.commit()

        cleared = await jwt_rotation.clear_expired_previous(db)
        assert cleared is True

        _c2, previous_after, _r = await jwt_rotation.load_secrets(db)
        assert not previous_after


@pytest.mark.asyncio
async def test_jwt_rotate_requires_admin(client) -> None:
    """Non-admin callers get 403 on both routes."""
    # Register + log in a second, non-admin user.
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": "civilian",
            "email": "civ@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "civilian", "password": "password123"},
    )
    civ_jwt = login.json()["access_token"]

    rot = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {civ_jwt}"},
    )
    assert rot.status_code == 403, rot.text

    status = await client.get(
        "/api/admin/security/jwt/status",
        headers={"Authorization": f"Bearer {civ_jwt}"},
    )
    assert status.status_code == 403


@pytest.mark.asyncio
async def test_rotate_rejected_within_60s_cooldown(client) -> None:
    """A second rotation inside the cooldown returns 429 + Retry-After."""
    jwt_token = await _login_admin(client)

    # First rotation establishes the cooldown window.
    r1 = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert r1.status_code == 200, r1.text

    # Re-login so the second request carries a token signed with the
    # post-rotation current_secret (the env-fallback path also works,
    # but a fresh JWT keeps the test independent of the legacy chain).
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "tester", "password": "password123"},
    )
    fresh_jwt = login.json()["access_token"]

    r2 = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {fresh_jwt}"},
    )
    assert r2.status_code == 429, r2.text
    # Retry-After header set so the frontend can drive a countdown.
    retry_after_header = r2.headers.get("Retry-After")
    assert retry_after_header is not None
    assert int(retry_after_header) >= 1
    assert int(retry_after_header) <= jwt_rotation.ROTATE_COOLDOWN_SECONDS
    body = r2.json()
    # FastAPI wraps the dict detail under "detail".
    assert body["detail"]["retry_after"] >= 1


@pytest.mark.asyncio
async def test_rotate_allowed_after_60s_cooldown(client) -> None:
    """After the cooldown elapses (simulated), rotation succeeds again."""
    jwt_token = await _login_admin(client)

    r1 = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert r1.status_code == 200, r1.text

    # Force-age rotated_at past the 60s cooldown. Same trick scenario 5
    # uses for the 24h grace — keeps the test deterministic without
    # actually sleeping in CI.
    aged = (
        datetime.now(timezone.utc) - timedelta(seconds=jwt_rotation.ROTATE_COOLDOWN_SECONDS + 5)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with SessionLocal() as db:
        await set_config(
            db, group="jwt", key="rotated_at",
            value=aged, encrypted=False,
        )
        await db.commit()
    # Cache held the old rotated_at — drop it so rotate_secret re-reads
    # the freshly-aged row.
    jwt_rotation.reset_cache_for_tests()

    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "tester", "password": "password123"},
    )
    fresh_jwt = login.json()["access_token"]
    r2 = await client.post(
        "/api/admin/security/jwt/rotate",
        headers={"Authorization": f"Bearer {fresh_jwt}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["rotated_at"]
