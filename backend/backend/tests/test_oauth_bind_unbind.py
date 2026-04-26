"""Tests for the bind / unbind / set-password GitHub account flows.

Scope (all ten covered in this file):
  1.  bind-start returns 302 + state cookie encoding ``intent=link``
  2.  bind callback with tampered state rejected (302 with bind_error)
  3.  bind callback happy path attaches github_id to the caller's user
  4.  bind collision (same github_id already on a different user) → 302
      bind_error=github_already_linked
  5.  unlink happy path for a local+github user → 204, fields cleared
  6.  unlink blocked when password_hash IS NULL → 409 with message
      mentioning "password"
  7.  set-password for a github-only user works + unlink then allowed
  8.  set-password rejected when password_hash already set → 409
  9.  bind-start requires authentication (401 without a JWT)
 10.  audit rows are written for oauth_link / oauth_unlink /
      password_set_for_oauth_user

These tests reuse the in-memory SQLite + ``oauth_client``-style fixture
pattern from ``test_oauth_smoke.py`` / ``test_oauth_integration.py`` so
behaviour under the full app factory is exercised end-to-end.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio

from backend.api.oauth import (
    STATE_COOKIE_NAME,
    _build_nonce,
    _parse_nonce_intent,
    _sign_state,
    _verify_state,
)
from backend.config import get_settings


# ---------------------------------------------------------------------------
# Mock GitHub transport (same shape as the existing OAuth tests)
# ---------------------------------------------------------------------------

GITHUB_USER_PAYLOAD: dict[str, Any] = {
    "id": 777777,
    "login": "bindercat",
    "name": "Binder Cat",
    "email": "bindercat@github-users.local",
}
GITHUB_EMAILS_PAYLOAD = [
    {"email": "bindercat@example.com", "primary": True, "verified": True},
]


def _build_mock_transport(
    *,
    user_resp: dict | None = None,
    emails_resp: list | None = None,
) -> httpx.MockTransport:
    user_resp = user_resp if user_resp is not None else GITHUB_USER_PAYLOAD
    emails_resp = emails_resp if emails_resp is not None else GITHUB_EMAILS_PAYLOAD

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "login/oauth/access_token" in url:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(
                    {"access_token": "gho_mock", "token_type": "bearer"}
                ).encode(),
            )
        if url.endswith("/user"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(user_resp).encode(),
            )
        if url.endswith("/user/emails"):
            return httpx.Response(
                200,
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


# ---------------------------------------------------------------------------
# Client fixture: OAuth-enabled, starts with a local user "alice" logged in
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bind_client(monkeypatch):
    """Yield a client logged in as a freshly-registered local user.

    The auth store is pre-populated with a JWT so ``Authorization:
    Bearer <jwt>`` is attached by default — the link-start endpoint
    depends on it. Tests that need to exercise the unauth path can
    pop the header for one call.
    """
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
            reg = await ac.post(
                "/api/auth/register",
                json={
                    "username": "alice",
                    "email": "alice@example.com",
                    "password": "password123",
                },
            )
            assert reg.status_code == 201, reg.text
            login = await ac.post(
                "/api/auth/login",
                json={
                    "username_or_email": "alice",
                    "password": "password123",
                },
            )
            assert login.status_code == 200
            jwt = login.json()["access_token"]
            ac.headers.update({"Authorization": f"Bearer {jwt}"})
            ac._test_jwt = jwt  # type: ignore[attr-defined]
            ac._test_username = "alice"  # type: ignore[attr-defined]
            yield ac

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_user_id(client, jwt: str | None = None) -> int:
    headers = {"Authorization": f"Bearer {jwt}"} if jwt else None
    r = await client.get("/api/auth/me", headers=headers)
    assert r.status_code == 200
    return int(r.json()["id"])


async def _start_link_and_grab_state(client) -> tuple[str, str]:
    """Hit /link/start (auth'd); return (state_param, cookie_nonce)."""
    r = await client.get("/api/auth/oauth/github/link/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    q = parse_qs(urlparse(loc).query)
    state = q["state"][0]
    nonce = r.cookies.get(STATE_COOKIE_NAME)
    assert nonce
    return state, nonce


# ===========================================================================
# 1. /link/start emits intent=link in the nonce + sets cookie
# ===========================================================================


@pytest.mark.asyncio
async def test_link_start_encodes_intent_link(bind_client):
    my_id = await _get_user_id(bind_client)
    r = await bind_client.get(
        "/api/auth/oauth/github/link/start", follow_redirects=False
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "github.com/login/oauth/authorize" in loc

    nonce = r.cookies.get(STATE_COOKIE_NAME)
    assert nonce, "state cookie must be set"
    intent, uid = _parse_nonce_intent(nonce)
    assert intent == "link"
    assert uid == my_id, "nonce must bind to the caller's user id"
    # allow_signup is off on the bind flow — we don't want GitHub to
    # offer creating a new account there.
    assert "allow_signup=false" in loc


# ===========================================================================
# 2. bind callback with tampered state rejected
# ===========================================================================


@pytest.mark.asyncio
async def test_link_callback_rejects_tampered_state(bind_client, monkeypatch):
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_link_and_grab_state(bind_client)
    tampered = state[:-1] + ("Z" if state[-1] != "Z" else "Y")
    r = await bind_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": tampered},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    # Link-flow errors land on /login/oauth/complete?bind_error=<reason>
    assert "/login/oauth/complete" in loc
    assert "bind_error=state_invalid" in loc


# ===========================================================================
# 3. bind callback happy path — attach github_id to caller
# ===========================================================================


@pytest.mark.asyncio
async def test_link_callback_happy_path_attaches_github(bind_client, monkeypatch):
    my_id = await _get_user_id(bind_client)
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_link_and_grab_state(bind_client)
    r = await bind_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "/login/oauth/complete" in loc
    assert "bind_ok=1" in loc
    # A JWT must NOT be minted for the link flow (we stay as alice).
    assert "#token=" not in loc

    me = await bind_client.get("/api/auth/me")
    body = me.json()
    assert body["id"] == my_id, "must stay logged in as the original user"
    assert body["username"] == "alice"
    assert body["github_login"] == "bindercat"
    assert body["has_password"] is True


# ===========================================================================
# 4. bind collision: github_id already linked to someone else
# ===========================================================================


@pytest.mark.asyncio
async def test_link_callback_rejects_github_id_already_linked(
    bind_client, monkeypatch
):
    """Register bob, link github_id=777777 to bob, then alice attempts the same.

    Alice's link must fail with ``bind_error=github_already_linked`` and
    her row must remain unlinked.
    """
    # Create bob and log in as bob to bind the github id to him first.
    r_reg = await bind_client.post(
        "/api/auth/register",
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": "password123",
        },
    )
    assert r_reg.status_code == 201
    login = await bind_client.post(
        "/api/auth/login",
        json={"username_or_email": "bob", "password": "password123"},
    )
    bob_jwt = login.json()["access_token"]

    alice_jwt = bind_client._test_jwt  # saved by the fixture

    # Bob's bind.
    bind_client.headers.update({"Authorization": f"Bearer {bob_jwt}"})
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_link_and_grab_state(bind_client)
    r1 = await bind_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert r1.status_code == 302
    assert "bind_ok=1" in r1.headers["location"]

    # Now alice tries to bind the same github account.
    bind_client.headers.update({"Authorization": f"Bearer {alice_jwt}"})
    _install_github_transport(monkeypatch, _build_mock_transport())
    state2, nonce2 = await _start_link_and_grab_state(bind_client)
    r2 = await bind_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state2},
        cookies={STATE_COOKIE_NAME: nonce2},
        follow_redirects=False,
    )
    assert r2.status_code == 302
    loc = r2.headers["location"]
    assert "/login/oauth/complete" in loc
    assert "bind_error=github_already_linked" in loc

    # Alice still unlinked.
    me = await bind_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {alice_jwt}"}
    )
    assert me.json().get("github_login") in (None, "")


# ===========================================================================
# 5. unlink happy path (user has password)
# ===========================================================================


@pytest.mark.asyncio
async def test_unlink_happy_path(bind_client, monkeypatch):
    # First link so there's something to unlink.
    _install_github_transport(monkeypatch, _build_mock_transport())
    state, nonce = await _start_link_and_grab_state(bind_client)
    await bind_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    me = (await bind_client.get("/api/auth/me")).json()
    assert me["github_login"] == "bindercat"

    # Unlink.
    r = await bind_client.post("/api/auth/oauth/github/unlink")
    assert r.status_code == 204, r.text

    me2 = (await bind_client.get("/api/auth/me")).json()
    assert me2.get("github_login") in (None, "")
    assert me2["has_password"] is True


# ===========================================================================
# 6. unlink blocked for github-only user with no password
# ===========================================================================


@pytest.mark.asyncio
async def test_unlink_blocked_without_password(monkeypatch):
    """Provision a github-only user, then attempt unlink → 409.

    We bypass the registration path and set up the user via the login
    OAuth callback (which creates ``password_hash=NULL`` rows for brand
    new GitHub identities).
    """
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
            _install_github_transport(monkeypatch, _build_mock_transport())
            # Login-flow OAuth — creates brand-new user with password_hash=None.
            r = await ac.get(
                "/api/auth/oauth/github/start", follow_redirects=False
            )
            state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
            nonce = r.cookies.get(STATE_COOKIE_NAME)
            cb = await ac.get(
                "/api/auth/oauth/github/callback",
                params={"code": "abc", "state": state},
                cookies={STATE_COOKIE_NAME: nonce},
                follow_redirects=False,
            )
            assert cb.status_code == 302
            fragment = cb.headers["location"].split("#", 1)[1]
            jwt = parse_qs(fragment)["token"][0]

            me = await ac.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {jwt}"}
            )
            assert me.json()["has_password"] is False

            # Attempt unlink — must 409.
            unlink = await ac.post(
                "/api/auth/oauth/github/unlink",
                headers={"Authorization": f"Bearer {jwt}"},
            )
            assert unlink.status_code == 409
            detail = unlink.json().get("detail", "")
            assert "password" in detail.lower(), (
                f"detail must mention password, got: {detail!r}"
            )
    get_settings.cache_clear()


# ===========================================================================
# 7. set-password for github-only, then unlink works
# ===========================================================================


@pytest.mark.asyncio
async def test_set_password_then_unlink(monkeypatch):
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
            _install_github_transport(monkeypatch, _build_mock_transport())
            r = await ac.get(
                "/api/auth/oauth/github/start", follow_redirects=False
            )
            state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
            nonce = r.cookies.get(STATE_COOKIE_NAME)
            cb = await ac.get(
                "/api/auth/oauth/github/callback",
                params={"code": "abc", "state": state},
                cookies={STATE_COOKIE_NAME: nonce},
                follow_redirects=False,
            )
            jwt = parse_qs(cb.headers["location"].split("#", 1)[1])["token"][0]
            headers = {"Authorization": f"Bearer {jwt}"}

            # Set a password on the github-only user.
            sp = await ac.post(
                "/api/auth/oauth/github/set-password",
                json={"new_password": "newpassword9"},
                headers=headers,
            )
            assert sp.status_code == 204, sp.text

            # /me.has_password now true.
            me = await ac.get("/api/auth/me", headers=headers)
            assert me.json()["has_password"] is True

            # Unlink now allowed.
            unlink = await ac.post(
                "/api/auth/oauth/github/unlink", headers=headers
            )
            assert unlink.status_code == 204

            # Can log in with the new password going forward.
            username = me.json()["username"]
            login = await ac.post(
                "/api/auth/login",
                json={"username_or_email": username, "password": "newpassword9"},
            )
            assert login.status_code == 200
    get_settings.cache_clear()


# ===========================================================================
# 8. set-password rejected when user already has one
# ===========================================================================


@pytest.mark.asyncio
async def test_set_password_rejected_when_already_set(bind_client):
    # alice already has password123 from the fixture.
    r = await bind_client.post(
        "/api/auth/oauth/github/set-password",
        json={"new_password": "anotherpass9"},
    )
    assert r.status_code == 409
    detail = r.json().get("detail", "").lower()
    assert "password" in detail


# ===========================================================================
# 9. bind-start requires authentication
# ===========================================================================


@pytest.mark.asyncio
async def test_link_start_requires_auth(bind_client):
    # Drop the default Authorization header for this call.
    original = bind_client.headers.pop("Authorization", None)
    try:
        r = await bind_client.get(
            "/api/auth/oauth/github/link/start", follow_redirects=False
        )
        assert r.status_code == 401
    finally:
        if original:
            bind_client.headers.update({"Authorization": original})


# ===========================================================================
# 10. audit rows written for link / unlink / set-password
# ===========================================================================


@pytest.mark.asyncio
async def test_audit_rows_written_for_link_unlink_setpw(monkeypatch):
    """Provision github-only user → set-password → unlink. Then audit log
    must contain oauth_unlink + password_set_for_oauth_user rows. Bind
    audit (oauth_link) is not exercised here because the github-only
    user was created via the login callback (which writes
    login_success, not oauth_link); a separate sub-case covers bind.
    """
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
            # 1) Register alice (password user).
            await ac.post(
                "/api/auth/register",
                json={
                    "username": "alice",
                    "email": "alice@example.com",
                    "password": "password123",
                },
            )
            login = await ac.post(
                "/api/auth/login",
                json={"username_or_email": "alice", "password": "password123"},
            )
            alice_jwt = login.json()["access_token"]
            alice_headers = {"Authorization": f"Bearer {alice_jwt}"}

            # 2) Bind → writes oauth_link.
            _install_github_transport(monkeypatch, _build_mock_transport())
            r = await ac.get(
                "/api/auth/oauth/github/link/start",
                headers=alice_headers,
                follow_redirects=False,
            )
            state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
            nonce = r.cookies.get(STATE_COOKIE_NAME)
            await ac.get(
                "/api/auth/oauth/github/callback",
                params={"code": "abc", "state": state},
                cookies={STATE_COOKIE_NAME: nonce},
                follow_redirects=False,
            )

            # 3) Unlink → writes oauth_unlink.
            r_unlink = await ac.post(
                "/api/auth/oauth/github/unlink", headers=alice_headers
            )
            assert r_unlink.status_code == 204

            # 4) Provision a github-only user via login flow, give them a
            #    password → writes password_set_for_oauth_user.
            _install_github_transport(
                monkeypatch,
                _build_mock_transport(
                    user_resp={
                        **GITHUB_USER_PAYLOAD,
                        "id": 111222,
                        "login": "ghonlycat",
                    },
                    emails_resp=[
                        {
                            "email": "ghonly@example.com",
                            "primary": True,
                            "verified": True,
                        }
                    ],
                ),
            )
            r = await ac.get(
                "/api/auth/oauth/github/start", follow_redirects=False
            )
            state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
            nonce = r.cookies.get(STATE_COOKIE_NAME)
            cb = await ac.get(
                "/api/auth/oauth/github/callback",
                params={"code": "abc", "state": state},
                cookies={STATE_COOKIE_NAME: nonce},
                follow_redirects=False,
            )
            gh_jwt = parse_qs(cb.headers["location"].split("#", 1)[1])["token"][0]
            await ac.post(
                "/api/auth/oauth/github/set-password",
                json={"new_password": "ghonlypass9"},
                headers={"Authorization": f"Bearer {gh_jwt}"},
            )

            # 5) Read the audit log (alice is first user → admin).
            audit = await ac.get(
                "/api/admin/audit-log", headers=alice_headers
            )
            assert audit.status_code == 200
            actions = {row["action"] for row in audit.json()}
            assert "oauth_link" in actions, (
                f"expected oauth_link; got {actions}"
            )
            assert "oauth_unlink" in actions, (
                f"expected oauth_unlink; got {actions}"
            )
            assert "password_set_for_oauth_user" in actions, (
                f"expected password_set_for_oauth_user; got {actions}"
            )
    get_settings.cache_clear()


# ===========================================================================
# 11. POST /github/link/init returns JSON authorize_url with state nonce
# ===========================================================================


@pytest.mark.asyncio
async def test_link_init_returns_authorize_url_with_state_nonce(bind_client):
    """Authenticated POST to /link/init must return JSON with authorize_url
    containing a valid signed state that encodes intent=link + user_id."""
    my_id = await _get_user_id(bind_client)

    r = await bind_client.post("/api/auth/oauth/github/link/init")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "authorize_url" in body, f"expected authorize_url in response, got {body}"

    url = body["authorize_url"]
    assert "github.com/login/oauth/authorize" in url
    assert "allow_signup=false" in url

    # State cookie must be set on the response.
    nonce = r.cookies.get(STATE_COOKIE_NAME)
    assert nonce, "state cookie must be present on /link/init response"

    # State param in the URL must verify against the nonce.
    from urllib.parse import parse_qs, urlparse
    from backend.config import get_settings
    settings = get_settings()
    q = parse_qs(urlparse(url).query)
    state_param = q["state"][0]
    assert _verify_state(state_param, nonce, settings.jwt_secret), (
        "state param in authorize_url must HMAC-verify against the nonce cookie"
    )

    # Nonce must encode intent=link and the caller's user_id.
    intent, uid = _parse_nonce_intent(nonce)
    assert intent == "link"
    assert uid == my_id, f"nonce user_id {uid!r} != expected {my_id!r}"


# ===========================================================================
# 12. POST /github/link/init returns 401 when not authenticated
# ===========================================================================


@pytest.mark.asyncio
async def test_link_init_401_when_not_authenticated(bind_client):
    """Calling /link/init without a bearer token must return 401."""
    original = bind_client.headers.pop("Authorization", None)
    try:
        r = await bind_client.post("/api/auth/oauth/github/link/init")
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
    finally:
        if original:
            bind_client.headers.update({"Authorization": original})


# ===========================================================================
# 13. Full round-trip: /link/init → simulated callback → user.github_id set
# ===========================================================================


@pytest.mark.asyncio
async def test_callback_with_link_nonce_links_current_user(bind_client, monkeypatch):
    """Init → extract nonce + state from JSON response → simulate GitHub
    callback → verify user row now has github_id attached."""
    my_id = await _get_user_id(bind_client)
    _install_github_transport(monkeypatch, _build_mock_transport())

    # Step 1: call /link/init as the authenticated user.
    r = await bind_client.post("/api/auth/oauth/github/link/init")
    assert r.status_code == 200, r.text
    authorize_url = r.json()["authorize_url"]
    nonce = r.cookies.get(STATE_COOKIE_NAME)
    assert nonce

    # Step 2: extract the state param from the authorize_url.
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(authorize_url).query)["state"][0]

    # Step 3: simulate GitHub redirecting back to our callback.
    cb = await bind_client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "abc", "state": state},
        cookies={STATE_COOKIE_NAME: nonce},
        follow_redirects=False,
    )
    assert cb.status_code == 302, cb.text
    loc = cb.headers["location"]
    assert "/login/oauth/complete" in loc
    assert "bind_ok=1" in loc
    assert "#token=" not in loc  # no JWT mint on link flow

    # Step 4: verify the user row is updated.
    me = await bind_client.get("/api/auth/me")
    body = me.json()
    assert body["id"] == my_id
    assert body["github_login"] == "bindercat"
