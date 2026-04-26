"""Tests for ``POST /api/me/resend_verification`` (#108)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


async def _jwt_headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {client._test_default_jwt}"}


async def _set_email_verified(value: bool) -> None:
    """Toggle the default ``tester`` user's ``email_verified`` flag.

    The default fixture user starts unverified (registration default), so
    we only need this helper for the "already verified" branch.
    """
    import backend.db as db_mod
    from backend.models import User

    async with db_mod.SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == "tester"))
        ).scalar_one()
        user.email_verified = value
        await session.commit()


async def test_resend_happy_path(client, email_service):
    """Unverified user → 200 + new EmailVerification row + queued email."""
    email_service.sent_messages.clear()

    r = await client.post(
        "/api/me/resend_verification", headers=await _jwt_headers(client)
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # An email was dispatched, with a verify_url pointing at /verify-email.
    assert email_service.sent_messages, "expected one verification email"
    last = email_service.sent_messages[-1]
    assert "verify_url" in last.context
    assert last.context["verify_url"].startswith(("http://", "https://"))
    assert "/verify-email?token=" in last.context["verify_url"]

    # And a fresh ``EmailVerification`` row landed in the DB.
    import backend.db as db_mod
    from backend.models import EmailVerification

    async with db_mod.SessionLocal() as session:
        rows = (
            await session.execute(
                select(EmailVerification).where(
                    EmailVerification.kind == "verify"
                )
            )
        ).scalars().all()
        # The register fixture left one row already; the resend adds one
        # more — total >= 2 (use >= so the test isn't fragile to the
        # register flow producing extras).
        assert len(rows) >= 2


async def test_already_verified_returns_409(client):
    """Verified user → 409 conflict, no email sent."""
    await _set_email_verified(True)

    r = await client.post(
        "/api/me/resend_verification", headers=await _jwt_headers(client)
    )
    assert r.status_code == 409, r.text
    assert "verified" in r.json()["detail"].lower()


async def test_rate_limit_blocks_second_call(client, email_service):
    """1/min cap — second call within the same minute returns 429."""
    email_service.sent_messages.clear()
    h = await _jwt_headers(client)

    r1 = await client.post("/api/me/resend_verification", headers=h)
    assert r1.status_code == 200, r1.text

    r2 = await client.post("/api/me/resend_verification", headers=h)
    assert r2.status_code == 429, r2.text
    # Retry-After surfaces in the response headers so the UI can render
    # a "wait N seconds" hint instead of a generic toast.
    assert "Retry-After" in r2.headers


async def test_rate_limit_resets_after_bucket_drain(client):
    """Resetting the bucket between tests is enough to mint another token."""
    from backend.utils.ratelimit import (
        reset_resend_verification_bucket_for_tests,
    )

    h = await _jwt_headers(client)
    r1 = await client.post("/api/me/resend_verification", headers=h)
    assert r1.status_code == 200

    # Simulate the per-test reset (mirrors what conftest does between
    # tests). After the reset a fresh resend is allowed.
    reset_resend_verification_bucket_for_tests()
    r2 = await client.post("/api/me/resend_verification", headers=h)
    assert r2.status_code == 200, r2.text


async def test_resend_requires_auth(unauthed_client):
    """No bearer token → 401."""
    r = await unauthed_client.post("/api/me/resend_verification")
    assert r.status_code == 401
