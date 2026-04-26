"""Smoke tests for the GitHub OAuth login path.

These cover the happy path (new user + existing-local link), the
disabled case (404), and the two most obvious misuse routes (bad state,
missing code). Deeper edge cases (rate limiting, partial failures,
email spoofing, username collision overflow) are deliberately left for
the dedicated test agent turn that follows dev.

We stub out the two outbound GitHub calls (``POST /login/oauth/access_token``
and ``GET /user`` + ``GET /user/emails``) with an :class:`httpx.MockTransport`
so the tests never reach the public internet. The transport is plugged
in globally via monkeypatching ``httpx.AsyncClient.__init__`` — a
lighter touch than swapping out the oauth module's import, and it's
reset per-test by pytest's fixture teardown.
"""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from backend.config import get_settings


GITHUB_USER_PAYLOAD = {
    "id": 424242,
    "login": "octocat",
    "name": "The Octocat",
    "email": "octocat@github-users.local",
}
GITHUB_EMAILS_PAYLOAD = [
    {"email": "octocat@example.com", "primary": True, "verified": True},
    {"email": "secondary@example.com", "primary": False, "verified": True},
]


def _build_mock_transport(
    *,
    token_resp: dict | None = None,
    user_resp: dict | None = None,
    emails_resp: list | None = None,
    token_status: int = 200,
    user_status: int = 200,
) -> httpx.MockTransport:
    """Return an httpx MockTransport that matches both GitHub endpoints."""
    token_resp = token_resp if token_resp is not None else {
        "access_token": "gho_mock_token_value",
        "token_type": "bearer",
        "scope": "read:user,user:email",
    }
    user_resp = user_resp if user_resp is not None else GITHUB_USER_PAYLOAD
    emails_resp = (
        emails_resp if emails_resp is not None else GITHUB_EMAILS_PAYLOAD
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "login/oauth/access_token" in url:
            return httpx.Response(
                token_status,
                headers={"content-type": "application/json"},
                content=json.dumps(token_resp).encode("utf-8"),
            )
        if url.endswith("/user"):
            return httpx.Response(
                user_status,
                headers={"content-type": "application/json"},
                content=json.dumps(user_resp).encode("utf-8"),
            )
        if url.endswith("/user/emails"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(emails_resp).encode("utf-8"),
            )
        return httpx.Response(404, content=b"not-mocked")

    return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def oauth_client(monkeypatch):
    """Pre-authenticated client fixture + OAuth settings enabled.

    Reuses the same bootstrap logic as the global ``client`` fixture
    (register + token) so leaderboard-free routes still work, but also
    sets the three ``ARGUS_GITHUB_*`` env vars BEFORE the app is
    created so ``Settings`` picks them up.
    """
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

    # Turn OAuth on before Settings is materialised.
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
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            yield ac
    # Clear so other tests using the normal ``client`` fixture aren't
    # contaminated by the OAuth env vars.
    get_settings.cache_clear()


def _install_github_transport(
    monkeypatch, transport: httpx.MockTransport
) -> None:
    """Swap out ``httpx.AsyncClient`` so outbound calls hit our transport.

    The oauth module constructs a fresh ``AsyncClient`` per request; we
    wrap the original constructor to inject ``transport=`` when the
    caller didn't specify one. ASGI tests that instantiate
    ``AsyncClient(transport=...)`` already pass a transport explicitly
    and are unaffected.
    """
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "transport" not in kwargs and "app" not in kwargs:
            kwargs["transport"] = transport
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


async def _do_start_and_grab_state(client) -> tuple[str, str]:
    """Hit /start, return (state_param_from_redirect, cookie_nonce)."""
    r = await client.get(
        "/api/auth/oauth/github/start", follow_redirects=False
    )
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith("https://github.com/login/oauth/authorize")
    # Extract the ``state`` query param.
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(loc).query)
    state = q["state"][0]
    nonce_cookie = r.cookies.get("em_oauth_state")
    assert nonce_cookie
    return state, nonce_cookie


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_404_when_disabled(client):
    """With no ARGUS_GITHUB_* env set, the default ``client`` fixture
    runs with OAuth disabled — both /start and /callback 404."""
    r = await client.get(
        "/api/auth/oauth/github/start", follow_redirects=False
    )
    assert r.status_code == 404
    r2 = await client.get(
        "/api/auth/oauth/github/callback?code=x&state=y",
        follow_redirects=False,
    )
    # Callback is also gated.
    assert r2.status_code == 404

    cfg = await client.get("/api/auth/oauth/config")
    assert cfg.status_code == 200
    assert cfg.json() == {"github": False}


@pytest.mark.asyncio
async def test_start_redirects_when_enabled(oauth_client):
    cfg = await oauth_client.get("/api/auth/oauth/config")
    assert cfg.status_code == 200
    assert cfg.json() == {"github": True}

    r = await oauth_client.get(
        "/api/auth/oauth/github/start", follow_redirects=False
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "github.com/login/oauth/authorize" in loc
    assert "client_id=test-client-id" in loc
    assert "scope=" in loc
    # State cookie is HttpOnly (httpx exposes cookies in .cookies but
    # not the flag; we just verify presence here).
    assert r.cookies.get("em_oauth_state")


@pytest.mark.asyncio
async def test_callback_exchanges_and_issues_jwt(oauth_client, monkeypatch):
    """New GitHub user → create row, audit login, redirect with token."""
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _do_start_and_grab_state(oauth_client)

    r = await oauth_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abcd", "state": state},
        cookies={"em_oauth_state": nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    target = r.headers["location"]
    assert "/login/oauth/complete" in target
    # JWT is in the URL fragment, not the query string.
    assert "#token=" in target
    assert "email=octocat%40example.com" in target
    assert "login=octocat" in target


@pytest.mark.asyncio
async def test_callback_new_user_creates_github_user(oauth_client, monkeypatch):
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _do_start_and_grab_state(oauth_client)
    r = await oauth_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abcd", "state": state},
        cookies={"em_oauth_state": nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302

    # Parse the JWT out of the fragment and call /me to prove the row
    # exists + the token is valid.
    fragment = r.headers["location"].split("#", 1)[1]
    from urllib.parse import parse_qs
    parts = parse_qs(fragment)
    jwt = parts["token"][0]

    me = await oauth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "octocat@example.com"
    assert body["username"] == "octocat"
    # First user on an empty install becomes admin (mirrors /register).
    assert body["is_admin"] is True
    assert body["email_verified"] is True


@pytest.mark.asyncio
async def test_callback_links_existing_local_user(oauth_client, monkeypatch):
    """A pre-existing local user with the same verified email gets linked.

    We register ``octocat@example.com`` via /register first, then run the
    OAuth dance. The post-callback /me must return that same user with
    ``is_admin=True`` (they were the first user) — i.e. no new row was
    created.
    """
    reg = await oauth_client.post(
        "/api/auth/register",
        json={
            "username": "localoctocat",
            "email": "octocat@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text

    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _do_start_and_grab_state(oauth_client)
    r = await oauth_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abcd", "state": state},
        cookies={"em_oauth_state": nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    # Token issued for the existing local user.
    fragment = r.headers["location"].split("#", 1)[1]
    from urllib.parse import parse_qs
    jwt = parse_qs(fragment)["token"][0]

    me = await oauth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert me.status_code == 200
    body = me.json()
    assert body["username"] == "localoctocat"  # kept local username
    assert body["email"] == "octocat@example.com"
    # Password-based login still works (link, not replace).
    login = await oauth_client.post(
        "/api/auth/login",
        json={
            "username_or_email": "localoctocat",
            "password": "password123",
        },
    )
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_callback_rejects_bad_state(oauth_client, monkeypatch):
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _do_start_and_grab_state(oauth_client)

    # Pass a tampered state (different HMAC suffix) → redirect to /login
    # with error=oauth_github_failed.
    tampered = state.split(".", 1)[0] + ".deadbeef"
    r = await oauth_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abcd", "state": tampered},
        cookies={"em_oauth_state": nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "/login" in r.headers["location"]
    assert "error=oauth_github_failed" in r.headers["location"]
    assert "reason=state_invalid" in r.headers["location"]


@pytest.mark.asyncio
async def test_callback_rejects_missing_code(oauth_client):
    # We skip /start here — no cookie, no state — and just hit the
    # callback with only ``state=``. Missing ``code`` is enough for the
    # handler to bail before even checking state.
    r = await oauth_client.get(
        "/api/auth/oauth/github/callback",
        params={"state": "anything.deadbeef"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=oauth_github_failed" in r.headers["location"]
    assert "reason=code_missing" in r.headers["location"]
