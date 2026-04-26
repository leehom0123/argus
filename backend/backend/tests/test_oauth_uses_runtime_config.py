"""N7 fix: OAuth call sites must read from ``system_config`` (DB) rather
than the static ``Settings.github_oauth_*`` properties.

The DB-driven config is the documented source of truth for v0.1.4
(operators edit it from Settings → Admin without a redeploy). Without
this migration, the `/api/auth/oauth/github/start` endpoint kept
returning 404 for any deployment where the admin hadn't *also* set the
matching `ARGUS_GITHUB_*` env vars — the entire point of the new
Settings page was to make env vars optional, so the UX was broken.
"""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from backend.config import get_settings


@pytest_asyncio.fixture
async def runtime_config_oauth_client(monkeypatch):
    """Boot the app with OAuth env vars unset.

    The ``ARGUS_GITHUB_*`` env vars are explicitly cleared so the only
    way OAuth lights up is via a ``system_config`` row written through
    the admin API.  This is the failure mode the N7 fix targets.
    """
    import os
    from httpx import ASGITransport, AsyncClient

    import backend.db as db_mod
    from backend import models  # noqa: F401
    from backend.app import create_app
    from backend.auth.jwt import clear_blacklist_for_tests
    from backend.services.email import reset_email_service_for_tests
    from backend.services.secrets import reset_for_tests as reset_secrets
    from backend.utils.ratelimit import (
        reset_default_bucket_for_tests,
        reset_public_bucket_for_tests,
    )

    monkeypatch.delenv("ARGUS_GITHUB_OAUTH_ENABLED", raising=False)
    monkeypatch.delenv("ARGUS_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("ARGUS_GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("ARGUS_BASE_URL", "http://localhost:5173")

    # ``get_settings`` is wrapped in :func:`functools.lru_cache` so a
    # ``Settings`` instance is built once per process from whatever env
    # was visible at first call. Other test modules — or the import-time
    # boot of this very module — may have populated that cache while
    # ``ARGUS_GITHUB_*`` env vars were still set, so without this clear
    # the fixture's ``monkeypatch.delenv`` calls above would be a no-op
    # in practice and the OAuth-disabled-by-env precondition this test
    # depends on would silently flip back to "enabled". Re-clearing in
    # the teardown below restores the cache for follow-up tests.
    get_settings.cache_clear()
    reset_email_service_for_tests()
    reset_secrets()
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
            # Provision a default admin user.
            reg = await ac.post(
                "/api/auth/register",
                json={
                    "username": "admin1",
                    "email": "admin1@example.com",
                    "password": "password123",
                },
            )
            assert reg.status_code == 201, reg.text
            login = await ac.post(
                "/api/auth/login",
                json={
                    "username_or_email": "admin1",
                    "password": "password123",
                },
            )
            assert login.status_code == 200
            jwt = login.json()["access_token"]
            ac.headers.update({"Authorization": f"Bearer {jwt}"})
            ac._test_default_jwt = jwt  # type: ignore[attr-defined]
            yield ac
    get_settings.cache_clear()


def _build_mock_transport() -> httpx.MockTransport:
    """Reuse the same GitHub-mock as test_oauth_smoke."""
    user_payload = {
        "id": 7777,
        "login": "octofromdb",
        "email": "octofromdb@example.com",
    }
    emails_payload = [
        {
            "email": "octofromdb@example.com",
            "primary": True,
            "verified": True,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "login/oauth/access_token" in url:
            # Confirm the token-exchange call carried the *DB* client_id,
            # not the env-var one.  This is the load-bearing assertion of
            # the test: if a settings.* leak still exists, the client_id
            # here would be empty.
            body = request.read().decode("utf-8")
            assert "client_id=client-id-from-db" in body, body
            assert "client_secret=secret-from-db" in body, body
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps({
                    "access_token": "gho_db_test",
                    "token_type": "bearer",
                    "scope": "read:user,user:email",
                }).encode("utf-8"),
            )
        if url.endswith("/user"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(user_payload).encode("utf-8"),
            )
        if url.endswith("/user/emails"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(emails_payload).encode("utf-8"),
            )
        return httpx.Response(404, content=b"not-mocked")

    return httpx.MockTransport(handler)


def _install_github_transport(
    monkeypatch, transport: httpx.MockTransport
) -> None:
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "transport" not in kwargs and "app" not in kwargs:
            kwargs["transport"] = transport
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_uses_get_config_not_settings(
    runtime_config_oauth_client, monkeypatch
):
    """End-to-end: writing the DB row alone is enough to enable OAuth.

    Env vars are unset.  We PUT both ``oauth.github_client_id`` and
    ``oauth.github_client_secret`` through the admin API, then verify:

    1. ``/api/auth/oauth/config`` flips ``github`` to True.
    2. ``/api/auth/oauth/github/start`` 302s to GitHub with the DB
       ``client_id`` in the query string (no env var was set, so a
       stale ``settings.*`` read would yield an empty value).
    3. The token-exchange POST carries the DB-sourced credentials
       (asserted inside the mock transport handler).
    """
    client = runtime_config_oauth_client

    # Pre-condition: with no env + no DB row, OAuth is OFF (404 + config
    # reports github=false). Confirms the harness is set up correctly.
    cfg = await client.get("/api/auth/oauth/config")
    assert cfg.status_code == 200
    assert cfg.json() == {"github": False}
    r = await client.get(
        "/api/auth/oauth/github/start", follow_redirects=False
    )
    assert r.status_code == 404, r.text

    # Write DB rows via the admin API.
    r = await client.put(
        "/api/admin/system-config/oauth/github_client_id",
        json={"value": "client-id-from-db"},
    )
    assert r.status_code == 200, r.text
    r = await client.put(
        "/api/admin/system-config/oauth/github_client_secret",
        json={"value": "secret-from-db"},
    )
    assert r.status_code == 200, r.text

    # Now OAuth must light up — purely from the DB.
    cfg = await client.get("/api/auth/oauth/config")
    assert cfg.status_code == 200
    assert cfg.json() == {"github": True}

    # /start carries client-id-from-db in the redirect URL.
    r = await client.get(
        "/api/auth/oauth/github/start", follow_redirects=False
    )
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert "client_id=client-id-from-db" in loc, loc
    nonce_cookie = r.cookies.get("em_oauth_state")
    assert nonce_cookie

    # Pull the state out so we can drive the callback.
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(loc).query)["state"][0]

    # Drive the callback, asserting the token-exchange call carries
    # the DB credentials (handler asserts inside).
    _install_github_transport(monkeypatch, _build_mock_transport())
    cb = await client.get(
        "/api/auth/oauth/github/callback",
        params={"code": "test-code", "state": state},
        cookies={"em_oauth_state": nonce_cookie},
        follow_redirects=False,
    )
    assert cb.status_code == 302, cb.text
    assert "/login/oauth/complete" in cb.headers["location"]


@pytest.mark.asyncio
async def test_oauth_db_takes_precedence_over_env(
    runtime_config_oauth_client, monkeypatch
):
    """When BOTH env vars and DB rows exist, the DB row wins.

    Mirrors the precedence contract documented on
    :func:`backend.services.runtime_config.get_config`.
    """
    client = runtime_config_oauth_client

    # Stale env values that, if leaked, would land in the redirect URL.
    monkeypatch.setenv("ARGUS_GITHUB_CLIENT_ID", "stale-env-id")
    monkeypatch.setenv("ARGUS_GITHUB_CLIENT_SECRET", "stale-env-secret")

    # Fresh DB rows.
    await client.put(
        "/api/admin/system-config/oauth/github_client_id",
        json={"value": "client-id-from-db"},
    )
    await client.put(
        "/api/admin/system-config/oauth/github_client_secret",
        json={"value": "secret-from-db"},
    )

    r = await client.get(
        "/api/auth/oauth/github/start", follow_redirects=False
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "client_id=client-id-from-db" in loc, loc
    assert "stale-env-id" not in loc, loc
