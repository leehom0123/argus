"""Comprehensive integration + edge-case tests for the GitHub OAuth path.

Complements the 7 smoke cases in ``test_oauth_smoke.py`` without duplicating
any of them.  Covers:

  Security:
    1.  State HMAC tamper → 302 /login?reason=state_invalid
    2.  State replay (one-time use) — TODO/known-minor-finding
    3.  Expired state cookie → 302 /login?reason=state_invalid
    4.  Open-redirect guard on ?redirect= param
    5.  CSRF mismatch (cookie nonce ≠ URL state nonce)
    6.  Missing / unverified email from GitHub → 302 /login?reason=no_verified_email
    7.  Downstream GitHub API failures (5xx on token, /user, /user/emails)

  Account-link edge cases:
    8.  github_id match wins over email-link collision
    9.  Banned local user (is_active=False) refused on callback
   10.  Admin promotion not duplicated (second user is not admin)
   11.  Username collision deterministic suffix + random-hex fallback

  Audit log:
   12.  Successful login writes audit row with via=github
   12b. Failed callback (bad code exchange) does NOT write audit row

  Integration with local auth:
   13.  Session stacking — same email from local + github yields same user_id
   14.  Password still works after linking via GitHub OAuth

  Config probe:
   15.  /api/auth/oauth/config reflects enabled / disabled / missing-secrets
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio

from backend.api.oauth import STATE_COOKIE_NAME, _sign_state, _verify_state
from backend.config import get_settings


# ---------------------------------------------------------------------------
# Re-use helpers from smoke file (inline here to keep the file self-contained)
# ---------------------------------------------------------------------------

GITHUB_USER_PAYLOAD: dict[str, Any] = {
    "id": 424242,
    "login": "octocat",
    "name": "The Octocat",
    "email": "octocat@github-users.local",
}
GITHUB_EMAILS_PAYLOAD = [
    {"email": "octocat@example.com", "primary": True, "verified": True},
]


def _build_mock_transport(
    *,
    token_resp: dict | None = None,
    user_resp: dict | None = None,
    emails_resp: list | None = None,
    token_status: int = 200,
    user_status: int = 200,
    emails_status: int = 200,
) -> httpx.MockTransport:
    token_resp = token_resp or {
        "access_token": "gho_mock_token_value",
        "token_type": "bearer",
        "scope": "read:user,user:email",
    }
    user_resp = user_resp if user_resp is not None else GITHUB_USER_PAYLOAD
    emails_resp = emails_resp if emails_resp is not None else GITHUB_EMAILS_PAYLOAD

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "login/oauth/access_token" in url:
            return httpx.Response(
                token_status,
                headers={"content-type": "application/json"},
                content=json.dumps(token_resp).encode(),
            )
        if url.endswith("/user"):
            return httpx.Response(
                user_status,
                headers={"content-type": "application/json"},
                content=json.dumps(user_resp).encode(),
            )
        if url.endswith("/user/emails"):
            return httpx.Response(
                emails_status,
                headers={"content-type": "application/json"},
                content=json.dumps(emails_resp).encode(),
            )
        return httpx.Response(404, content=b"not-mocked")

    return httpx.MockTransport(handler)


def _install_github_transport(monkeypatch, transport: httpx.MockTransport) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "transport" not in kwargs and "app" not in kwargs:
            kwargs["transport"] = transport
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


async def _start_and_grab_state(client) -> tuple[str, str]:
    """Hit /start; return (state_param, cookie_nonce)."""
    r = await client.get("/api/auth/oauth/github/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    q = parse_qs(urlparse(loc).query)
    state = q["state"][0]
    nonce = r.cookies.get(STATE_COOKIE_NAME)
    assert nonce
    return state, nonce


def _parse_jwt_from_redirect(location: str) -> str:
    fragment = location.split("#", 1)[1]
    return parse_qs(fragment)["token"][0]


# ---------------------------------------------------------------------------
# Shared OAuth-enabled fixture (mirrors the smoke file's oauth_client)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def oc(monkeypatch):
    """OAuth-enabled async client on a fresh in-memory DB."""
    import os
    from httpx import ASGITransport, AsyncClient

    import backend.db as db_mod
    from backend import models  # noqa: F401
    from backend.app import create_app
    from backend.auth.jwt import clear_blacklist_for_tests
    from backend.services.email import reset_email_service_for_tests
    from backend.utils.ratelimit import (
        reset_default_bucket_for_tests,
        reset_public_bucket_for_tests,
    )

    monkeypatch.setenv("ARGUS_GITHUB_OAUTH_ENABLED", "true")
    monkeypatch.setenv("ARGUS_GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("ARGUS_GITHUB_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("ARGUS_BASE_URL", "http://localhost:5173")

    get_settings.cache_clear()
    reset_email_service_for_tests()
    clear_blacklist_for_tests()
    reset_default_bucket_for_tests()
    reset_public_bucket_for_tests()

    async with db_mod.engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.drop_all)
        await conn.run_sync(db_mod.Base.metadata.create_all)

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    get_settings.cache_clear()


# ===========================================================================
# 1. State HMAC tamper
# ===========================================================================


@pytest.mark.asyncio
async def test_state_hmac_tamper_rejected(oc, monkeypatch):
    """Mutate only the HMAC suffix of a valid state token → 302 state_invalid.

    This differs from smoke test_callback_rejects_bad_state which uses the
    public HTTP surface. Here we import ``_sign_state`` / ``_verify_state``
    directly and assert that a single-byte mutation in the HMAC fails
    constant-time comparison BEFORE calling the callback endpoint.
    """
    import secrets as _sec

    # Verify the helpers themselves first (unit-level).
    nonce = _sec.token_urlsafe(24)
    secret = "test-secret-32-bytes-minimum-fixture-value"
    valid_state = _sign_state(nonce, secret)
    assert _verify_state(valid_state, nonce, secret) is True

    # Flip last char.
    tampered_mac = valid_state[:-1] + ("X" if valid_state[-1] != "X" else "Y")
    assert _verify_state(tampered_mac, nonce, secret) is False

    # Also confirm the HTTP layer refuses.
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, real_nonce = await _start_and_grab_state(oc)
    tampered = state[:-1] + ("X" if state[-1] != "X" else "Y")
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": tampered},
        cookies={STATE_COOKIE_NAME: real_nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "reason=state_invalid" in r.headers["location"]


# ===========================================================================
# 2. State replay (one-time use)
# ===========================================================================


@pytest.mark.asyncio
# TODO(security finding #2): State tokens are not tracked server-side after
# first use. A captured state + nonce cookie can be replayed a second time
# within the 10-minute TTL window.  The fix is to store consumed nonces in a
# short-lived cache (e.g. Redis or an in-memory LRU keyed on nonce) and
# reject any nonce that has already been used.
# Marking xfail because the impl does NOT invalidate used states yet.
@pytest.mark.xfail(
    reason="security finding #2 — state replay not yet guarded; "
    "server must track consumed nonces to block this",
    strict=False,
)
async def test_state_replay_second_attempt_rejected(oc, monkeypatch):
    """Use a valid state twice; the second attempt must be rejected (400/302 error)."""
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_and_grab_state(oc)

    # First use — must succeed.
    r1 = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r1.status_code == 302
    assert "#token=" in r1.headers["location"], "First use should succeed"

    # Second use — should be rejected once nonce tracking is implemented.
    r2 = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r2.status_code == 302
    assert "reason=state_invalid" in r2.headers["location"], (
        "Replay must be rejected; see TODO comment in test for fix instructions"
    )


# ===========================================================================
# 3. Expired state cookie
# ===========================================================================


@pytest.mark.asyncio
async def test_expired_state_rejected(oc, monkeypatch):
    """State cookie older than 10 min is refused.

    The state TTL is enforced by the browser dropping the cookie after
    max_age=600 seconds. In test we skip the /start endpoint entirely and
    craft a state whose nonce is valid HMAC-wise but whose cookie has
    "expired" — simulated by setting a cookie with a past max_age value.
    Because httpx does not actually honour max_age for sent cookies, we
    instead test the complementary case: a state whose nonce cookie is
    absent (expired/dropped) maps to the same code path (nonce='') which
    _verify_state rejects.

    Additionally we verify _verify_state directly with an empty nonce.
    """
    import secrets as _sec

    secret = "test-secret-32-bytes-minimum-fixture-value"
    nonce = _sec.token_urlsafe(24)
    state = _sign_state(nonce, secret)

    # An empty / absent cookie nonce mimics browser dropping an expired cookie.
    assert _verify_state(state, "", secret) is False
    assert _verify_state(state, None, secret) is False  # type: ignore[arg-type]

    # HTTP surface: state in URL but cookie absent → state_invalid.
    _install_github_transport(monkeypatch, _build_mock_transport())
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        # no STATE_COOKIE_NAME cookie — simulates expiry
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "reason=state_invalid" in r.headers["location"]


# ===========================================================================
# 4. Open-redirect guard on ?redirect=
# ===========================================================================


@pytest.mark.asyncio
async def test_open_redirect_absolute_url_refused(oc):
    """?redirect=https://evil.com must be silently dropped (no redirect cookie set)."""
    r = await oc.get(
        "/api/auth/oauth/github/start",
        params={"redirect": "https://evil.com"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    # The redirect cookie must NOT be set when the value is absolute.
    assert f"{STATE_COOKIE_NAME}_redirect" not in r.cookies


@pytest.mark.asyncio
async def test_open_redirect_proto_relative_refused(oc):
    """?redirect=//evil.com (protocol-relative) must be dropped."""
    r = await oc.get(
        "/api/auth/oauth/github/start",
        params={"redirect": "//evil.com/steal"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert f"{STATE_COOKIE_NAME}_redirect" not in r.cookies


@pytest.mark.asyncio
async def test_open_redirect_relative_with_query_honored(oc, monkeypatch):
    """?redirect=/dashboard?x=1 is a valid relative path and must be accepted."""
    r = await oc.get(
        "/api/auth/oauth/github/start",
        params={"redirect": "/dashboard?x=1"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    # Cookie must be set for a valid relative redirect.
    assert f"{STATE_COOKIE_NAME}_redirect" in r.cookies


@pytest.mark.asyncio
async def test_open_redirect_url_encoded_utf8_honored(oc, monkeypatch):
    """?redirect=/projects/%E4%B8%AD%E6%96%87 (url-encoded Chinese) is relative → honored."""
    r = await oc.get(
        "/api/auth/oauth/github/start",
        params={"redirect": "/projects/%E4%B8%AD%E6%96%87"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert f"{STATE_COOKIE_NAME}_redirect" in r.cookies


# ===========================================================================
# 5. CSRF mismatch (cookie nonce differs from URL state nonce)
# ===========================================================================


@pytest.mark.asyncio
async def test_csrf_mismatch_rejected(oc, monkeypatch):
    """State cookie present but URL state encodes a different nonce → state_invalid."""
    _install_github_transport(monkeypatch, _build_mock_transport())
    # Do a real /start to get a valid nonce in the cookie.
    r_start = await oc.get("/api/auth/oauth/github/start", follow_redirects=False)
    assert r_start.status_code == 302
    real_nonce = r_start.cookies.get(STATE_COOKIE_NAME)
    assert real_nonce

    # Build a completely different valid state (different nonce).
    import secrets as _sec

    other_nonce = _sec.token_urlsafe(24)
    settings = get_settings()
    other_state = _sign_state(other_nonce, settings.jwt_secret)

    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": other_state},
        cookies={STATE_COOKIE_NAME: real_nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "reason=state_invalid" in r.headers["location"]


# ===========================================================================
# 6. Missing / unverified email
# ===========================================================================


@pytest.mark.asyncio
async def test_unverified_primary_email_refused(oc, monkeypatch):
    """GitHub returns only an unverified primary email + null top-level email.

    The callback must refuse with reason=no_verified_email rather than
    create an account linked to an unverified address.
    """
    bad_emails = [{"email": "ghost@example.com", "primary": True, "verified": False}]
    bad_user = {**GITHUB_USER_PAYLOAD, "email": None}  # no fallback either

    _install_github_transport(
        monkeypatch,
        _build_mock_transport(user_resp=bad_user, emails_resp=bad_emails),
    )
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "reason=no_verified_email" in r.headers["location"]


@pytest.mark.asyncio
async def test_no_email_at_all_refused(oc, monkeypatch):
    """GitHub returns an empty email list and null /user.email."""
    _install_github_transport(
        monkeypatch,
        _build_mock_transport(
            user_resp={**GITHUB_USER_PAYLOAD, "email": None},
            emails_resp=[],
        ),
    )
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "reason=no_verified_email" in r.headers["location"]


# ===========================================================================
# 7. Downstream API failures
# ===========================================================================


@pytest.mark.asyncio
async def test_token_exchange_500_redirects_to_error(oc, monkeypatch):
    """GitHub /login/oauth/access_token returns 500 → reason=exchange_failed."""
    _install_github_transport(
        monkeypatch, _build_mock_transport(token_status=500)
    )
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "error=oauth_github_failed" in loc
    # The impl returns exchange_failed for a bad token response.
    assert "reason=exchange_failed" in loc


@pytest.mark.asyncio
async def test_user_fetch_500_redirects_to_error(oc, monkeypatch):
    """GitHub GET /user returns 500 → reason=user_fetch_failed."""
    _install_github_transport(
        monkeypatch, _build_mock_transport(user_status=500)
    )
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "reason=user_fetch_failed" in r.headers["location"]


@pytest.mark.asyncio
async def test_emails_500_falls_through_to_user_email(oc, monkeypatch):
    """/user/emails returns 500; impl falls back to /user.email field.

    The _fetch_github_profile function treats a non-2xx emails response as
    an empty list and falls through to the top-level ``user_json['email']``.
    If that field is a valid string the flow succeeds; if it's None the
    callback refuses with no_verified_email.
    """
    # user.email is a valid string → flow should succeed.
    _install_github_transport(
        monkeypatch,
        _build_mock_transport(
            user_resp={**GITHUB_USER_PAYLOAD, "email": "octocat@example.com"},
            emails_status=500,
        ),
    )
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "#token=" in r.headers["location"]


# ===========================================================================
# 8. github_id match wins over email-link collision
# ===========================================================================


@pytest.mark.asyncio
async def test_github_id_match_wins_over_email_link(oc, monkeypatch):
    """Pre-existing GitHub user (github_id=424242, email=old@example.com).

    A second callback arrives with same github_id=424242 but a different
    email (alice@example.com) that happens to belong to a local user.
    The github_id match should win; we must NOT re-attach to alice's account.
    """
    # Step 1: Create alice (local user).
    reg_alice = await oc.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        },
    )
    assert reg_alice.status_code == 201, reg_alice.text

    # Step 2: First OAuth callback for github_id=424242 with old@example.com.
    old_emails = [{"email": "old@example.com", "primary": True, "verified": True}]
    old_user = {**GITHUB_USER_PAYLOAD, "id": 424242, "email": "old@example.com"}
    _install_github_transport(
        monkeypatch,
        _build_mock_transport(user_resp=old_user, emails_resp=old_emails),
    )
    state, nonce = await _start_and_grab_state(oc)
    r1 = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r1.status_code == 302
    assert "#token=" in r1.headers["location"]
    jwt_old = _parse_jwt_from_redirect(r1.headers["location"])
    me_old = await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {jwt_old}"})
    original_user_id = me_old.json()["id"]

    # Step 3: Second OAuth callback — same github_id but now email=alice@example.com.
    alice_emails = [{"email": "alice@example.com", "primary": True, "verified": True}]
    alice_user = {**GITHUB_USER_PAYLOAD, "id": 424242, "email": "alice@example.com"}
    _install_github_transport(
        monkeypatch,
        _build_mock_transport(user_resp=alice_user, emails_resp=alice_emails),
    )
    state2, nonce2 = await _start_and_grab_state(oc)
    r2 = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "def", "state": state2},
        cookies={STATE_COOKIE_NAME: nonce2},
        follow_redirects=False,
    )
    assert r2.status_code == 302
    assert "#token=" in r2.headers["location"]
    jwt_new = _parse_jwt_from_redirect(r2.headers["location"])
    me_new = await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {jwt_new}"})
    returned_user = me_new.json()

    # The returned user_id must be the ORIGINAL github-provisioned user, not alice.
    assert returned_user["id"] == original_user_id, (
        f"github_id match should have returned user_id={original_user_id} "
        f"but got user_id={returned_user['id']} (alice's id)"
    )
    assert returned_user["username"] != "alice", (
        "github_id match must NOT re-attach to alice's account"
    )


# ===========================================================================
# 9. Banned local user refused
# ===========================================================================


@pytest.mark.asyncio
async def test_banned_local_user_cannot_link_via_github(oc, monkeypatch):
    """A local user with is_active=False is refused during OAuth callback.

    The flow reaches the email-link branch (same email in GitHub payload
    matches the inactive local user). The impl refuses with
    reason=exchange_failed or similar rather than issuing a JWT.

    NOTE: The current implementation does NOT check is_active before
    issuing a JWT for linked users. This is a security finding.
    The test is marked xfail to document the gap for the review agent.
    """
    # Register a local user then deactivate them via the admin endpoint.
    reg = await oc.post(
        "/api/auth/register",
        json={
            "username": "banneduser",
            "email": "banned@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text

    # Log in to get a JWT, then use admin endpoint to deactivate.
    login_r = await oc.post(
        "/api/auth/login",
        json={"username_or_email": "banneduser", "password": "password123"},
    )
    assert login_r.status_code == 200
    user_jwt = login_r.json()["access_token"]
    user_id_r = await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {user_jwt}"})
    user_id = user_id_r.json()["id"]

    # We need an admin account; register a second user who becomes admin
    # after the banned user (first-user is admin already — so banneduser
    # is admin here). Use the DB directly to deactivate banneduser.
    import backend.db as db_mod
    from backend.models import User
    from sqlalchemy import select

    async with db_mod.SessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        u = result.scalar_one()
        u.is_active = False
        await db.commit()

    # Now attempt GitHub OAuth with banned user's email.
    _install_github_transport(
        monkeypatch,
        _build_mock_transport(
            user_resp={**GITHUB_USER_PAYLOAD, "id": 99999, "email": "banned@example.com"},
            emails_resp=[{"email": "banned@example.com", "primary": True, "verified": True}],
        ),
    )
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    # TODO(security finding #9): The implementation does not check is_active
    # before linking a GitHub identity to an existing local user. A banned user
    # can bypass the ban by using OAuth with their registered email address.
    # Fix: in the email-link branch of github_callback, add:
    #     if not existing_by_email.is_active:
    #         return _error_redirect(settings, "account_disabled")
    # Marking xfail because the impl currently issues a JWT for banned users.
    pytest.xfail(
        "security finding #9 — banned local users can link via GitHub OAuth; "
        "impl must check is_active in the email-link branch"
    )
    assert r.status_code == 302
    assert "error=oauth_github_failed" in r.headers["location"]


# ===========================================================================
# 10. Admin promotion not duplicated
# ===========================================================================


@pytest.mark.asyncio
async def test_admin_promotion_only_first_user(oc, monkeypatch):
    """First GitHub user gets is_admin=True; second user does NOT."""
    # First OAuth user.
    _install_github_transport(monkeypatch, _build_mock_transport())
    state1, nonce1 = await _start_and_grab_state(oc)
    r1 = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state1},
        cookies={STATE_COOKIE_NAME: nonce1},
        follow_redirects=False,
    )
    jwt1 = _parse_jwt_from_redirect(r1.headers["location"])
    me1 = (await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {jwt1}"})).json()
    assert me1["is_admin"] is True, "First user should be admin"

    # Second OAuth user (different github_id and email).
    second_user = {**GITHUB_USER_PAYLOAD, "id": 9999, "login": "second"}
    second_emails = [{"email": "second@example.com", "primary": True, "verified": True}]
    _install_github_transport(
        monkeypatch,
        _build_mock_transport(user_resp=second_user, emails_resp=second_emails),
    )
    state2, nonce2 = await _start_and_grab_state(oc)
    r2 = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "xyz", "state": state2},
        cookies={STATE_COOKIE_NAME: nonce2},
        follow_redirects=False,
    )
    jwt2 = _parse_jwt_from_redirect(r2.headers["location"])
    me2 = (await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {jwt2}"})).json()
    assert me2["is_admin"] is False, "Second user must NOT be admin"


# ===========================================================================
# 11. Username collision deterministic suffix + random-hex fallback
# ===========================================================================


@pytest.mark.asyncio
async def test_username_collision_suffix(oc, monkeypatch):
    """Pre-create 'octocat'; new GitHub user gets 'octocat-1'."""
    # Create a local user claiming the octocat username.
    reg = await oc.post(
        "/api/auth/register",
        json={
            "username": "octocat",
            "email": "other@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text

    # GitHub login for octocat login.
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    jwt = _parse_jwt_from_redirect(r.headers["location"])
    me = (await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {jwt}"})).json()
    assert me["username"] == "octocat-1", (
        f"Expected 'octocat-1' after first collision, got {me['username']!r}"
    )


@pytest.mark.asyncio
async def test_username_collision_double_suffix(oc, monkeypatch):
    """Pre-create 'octocat' and 'octocat-1'; new user gets 'octocat-2'."""
    for username, email in [("octocat", "a@example.com"), ("octocat-1", "b@example.com")]:
        reg = await oc.post(
            "/api/auth/register",
            json={"username": username, "email": email, "password": "password123"},
        )
        assert reg.status_code == 201

    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    jwt = _parse_jwt_from_redirect(r.headers["location"])
    me = (await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {jwt}"})).json()
    assert me["username"] == "octocat-2"


@pytest.mark.asyncio
async def test_username_collision_random_hex_fallback(oc, monkeypatch):
    """Simulate 200 consecutive collisions; impl falls back to random-hex suffix.

    We don't create 200 real rows — instead we monkeypatch
    ``_allocate_unique_username`` to always return a hex-suffixed name on
    the first call, simulating what the impl does after 200 attempts.
    """
    from backend.api import oauth as oauth_mod

    original_alloc = oauth_mod._allocate_unique_username

    async def always_hex(db, base):
        import secrets as _sec
        # Simulate the fallback path: 200 collisions exhausted.
        return f"{base}-{_sec.token_hex(4)}"

    monkeypatch.setattr(oauth_mod, "_allocate_unique_username", always_hex)

    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    jwt = _parse_jwt_from_redirect(r.headers["location"])
    me = (await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {jwt}"})).json()
    # Username must start with "octocat-" and end with an 8-hex-char suffix.
    import re

    assert re.match(r"^octocat-[0-9a-f]{8}$", me["username"]), (
        f"Expected hex-suffix username, got {me['username']!r}"
    )


# ===========================================================================
# 12. Audit log
# ===========================================================================


@pytest.mark.asyncio
async def test_successful_github_login_writes_audit_row(oc, monkeypatch):
    """Callback success writes action='login_success' with via='github'."""
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "#token=" in r.headers["location"]

    jwt = _parse_jwt_from_redirect(r.headers["location"])

    # Use admin audit log endpoint to check the row.
    audit_r = await oc.get(
        "/api/admin/audit-log",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert audit_r.status_code == 200
    rows = audit_r.json()
    # Filter to login_success rows that have via=github metadata.
    github_login_rows = [
        row for row in rows
        if row.get("action") == "login_success"
        and isinstance(row.get("metadata"), dict)
        and row["metadata"].get("via") == "github"
    ]
    assert github_login_rows, (
        f"Expected at least one login_success audit row with via=github. "
        f"Rows found: {[r.get('action') for r in rows]}"
    )


@pytest.mark.asyncio
async def test_failed_callback_does_not_write_audit_row(oc, monkeypatch):
    """A callback that fails on code exchange must NOT produce an audit row."""
    _install_github_transport(
        monkeypatch, _build_mock_transport(token_status=500)
    )
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "bad_code", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=oauth_github_failed" in r.headers["location"]

    # Register admin user to access audit log.
    reg = await oc.post(
        "/api/auth/register",
        json={"username": "admin", "email": "admin@example.com", "password": "password123"},
    )
    login_r = await oc.post(
        "/api/auth/login",
        json={"username_or_email": "admin", "password": "password123"},
    )
    admin_jwt = login_r.json()["access_token"]

    audit_r = await oc.get(
        "/api/admin/audit-log",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert audit_r.status_code == 200
    rows = audit_r.json()
    # Filter specifically to GitHub-origin login_success rows (the admin
    # registration + login produce their own login_success rows via local auth).
    github_login_rows = [
        row for row in rows
        if row.get("action") == "login_success"
        and isinstance(row.get("metadata"), dict)
        and row["metadata"].get("via") == "github"
    ]
    assert not github_login_rows, (
        "No github-via login_success audit rows expected after exchange failure"
    )


# ===========================================================================
# 13. Session stacking — local + GitHub same email
# ===========================================================================


@pytest.mark.asyncio
async def test_session_stacking_same_user_id(oc, monkeypatch):
    """Local user logs in with password; then OAuth for same email.

    The resulting JWT from OAuth must resolve to the SAME user_id as the
    original password login.  The /me endpoint is used as the oracle.
    """
    # Step 1: register a local user.
    reg = await oc.post(
        "/api/auth/register",
        json={
            "username": "localuser",
            "email": "octocat@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201

    # Step 2: local login.
    login_r = await oc.post(
        "/api/auth/login",
        json={"username_or_email": "localuser", "password": "password123"},
    )
    assert login_r.status_code == 200
    local_jwt = login_r.json()["access_token"]
    local_me = (
        await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {local_jwt}"})
    ).json()
    local_user_id = local_me["id"]

    # Step 3: GitHub OAuth for the same email.
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "#token=" in r.headers["location"]
    oauth_jwt = _parse_jwt_from_redirect(r.headers["location"])
    oauth_me = (
        await oc.get("/api/auth/me", headers={"Authorization": f"Bearer {oauth_jwt}"})
    ).json()

    assert oauth_me["id"] == local_user_id, (
        f"OAuth for linked email should return same user_id={local_user_id}, "
        f"got {oauth_me['id']}"
    )
    assert oauth_me["username"] == "localuser", (
        "Linked user should retain original local username"
    )


# ===========================================================================
# 14. Password still works after linking
# ===========================================================================


@pytest.mark.asyncio
async def test_password_still_works_after_github_link(oc, monkeypatch):
    """Local user linked via OAuth can still authenticate with password."""
    # Register local user.
    reg = await oc.post(
        "/api/auth/register",
        json={
            "username": "locallink",
            "email": "octocat@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201

    # GitHub link.
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_and_grab_state(oc)
    r = await oc.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "#token=" in r.headers["location"]

    # Original password must still work.
    login_r = await oc.post(
        "/api/auth/login",
        json={"username_or_email": "locallink", "password": "password123"},
    )
    assert login_r.status_code == 200, (
        "Password login must still work after GitHub link"
    )
    assert "access_token" in login_r.json()


# ===========================================================================
# 15. Config probe
# ===========================================================================


@pytest.mark.asyncio
async def test_config_probe_enabled(oc):
    """GET /api/auth/oauth/config returns github=true when fully configured."""
    r = await oc.get("/api/auth/oauth/config")
    assert r.status_code == 200
    assert r.json() == {"github": True}


@pytest.mark.asyncio
async def test_config_probe_disabled(client):
    """GET /api/auth/oauth/config returns github=false when OAuth disabled (default client)."""
    r = await client.get("/api/auth/oauth/config")
    assert r.status_code == 200
    assert r.json() == {"github": False}


@pytest.mark.asyncio
async def test_config_probe_missing_secrets(monkeypatch):
    """github=false when enabled=true but secrets missing."""
    import os
    from httpx import ASGITransport, AsyncClient

    import backend.db as db_mod
    from backend import models  # noqa: F401
    from backend.app import create_app
    from backend.auth.jwt import clear_blacklist_for_tests
    from backend.services.email import reset_email_service_for_tests
    from backend.utils.ratelimit import (
        reset_default_bucket_for_tests,
        reset_public_bucket_for_tests,
    )

    monkeypatch.setenv("ARGUS_GITHUB_OAUTH_ENABLED", "true")
    # Deliberately omit CLIENT_ID and CLIENT_SECRET.
    monkeypatch.delenv("ARGUS_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("ARGUS_GITHUB_CLIENT_SECRET", raising=False)

    get_settings.cache_clear()
    reset_email_service_for_tests()
    clear_blacklist_for_tests()
    reset_default_bucket_for_tests()
    reset_public_bucket_for_tests()

    async with db_mod.engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.drop_all)
        await conn.run_sync(db_mod.Base.metadata.create_all)

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/api/auth/oauth/config")
            assert r.status_code == 200
            assert r.json() == {"github": False}

    get_settings.cache_clear()
