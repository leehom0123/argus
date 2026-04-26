"""HTTP tests for ``/api/admin/system-config``."""
from __future__ import annotations

import pytest


async def _mk_user(client, username: str) -> str:
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
    )
    return lr.json()["access_token"]


def _admin_headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {client._test_default_jwt}"}


@pytest.mark.asyncio
async def test_get_all_returns_known_groups(client):
    """The aggregate endpoint surfaces every group."""
    r = await client.get(
        "/api/admin/system-config", headers=_admin_headers(client)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {
        "oauth", "smtp", "retention", "feature_flags", "demo"
    }
    # Every item carries a source label.
    for group, items in body.items():
        for it in items:
            assert it["source"] in ("db", "env", "default")


@pytest.mark.asyncio
async def test_non_admin_cannot_access(client):
    """403 for non-admin users."""
    bob_jwt = await _mk_user(client, "bob")
    for path in (
        "/api/admin/system-config",
        "/api/admin/system-config/oauth",
    ):
        r = await client.get(
            path, headers={"Authorization": f"Bearer {bob_jwt}"}
        )
        assert r.status_code == 403, f"{path} returned {r.status_code}"
    # PUT is also gated.
    r = await client.put(
        "/api/admin/system-config/oauth/github_enabled",
        json={"value": True},
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_put_writes_db_and_returns_typed_value(client):
    """A PUT round-trips through the DB."""
    r = await client.put(
        "/api/admin/system-config/retention/snapshot_days",
        json={"value": 42},
        headers=_admin_headers(client),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["value"] == 42
    assert body["source"] == "db"
    assert body["encrypted"] is False

    # Confirm via group GET.
    r = await client.get(
        "/api/admin/system-config/retention",
        headers=_admin_headers(client),
    )
    assert r.status_code == 200
    items = {it["key"]: it for it in r.json()}
    assert items["snapshot_days"]["value"] == 42
    assert items["snapshot_days"]["source"] == "db"


@pytest.mark.asyncio
async def test_secret_keys_are_masked_on_read(client):
    """OAuth client_secret comes back as ``"***"`` after a write."""
    r = await client.put(
        "/api/admin/system-config/oauth/github_client_secret",
        json={"value": "shhh-secret-value"},
        headers=_admin_headers(client),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The PUT response also masks — never echo plaintext to the browser.
    assert body["value"] == "***"
    assert body["encrypted"] is True

    # GET masks too.
    r = await client.get(
        "/api/admin/system-config/oauth", headers=_admin_headers(client)
    )
    items = {it["key"]: it for it in r.json()}
    assert items["github_client_secret"]["value"] == "***"
    assert items["github_client_secret"]["encrypted"] is True


@pytest.mark.asyncio
async def test_secret_mask_sentinel_preserves_existing_value(client):
    """PUT-ing the literal ``"***"`` back doesn't clobber the secret."""
    # Initial write
    r = await client.put(
        "/api/admin/system-config/smtp/password",
        json={"value": "real-password"},
        headers=_admin_headers(client),
    )
    assert r.status_code == 200

    # Re-PUT with the mask — should be a no-op
    r = await client.put(
        "/api/admin/system-config/smtp/password",
        json={"value": "***"},
        headers=_admin_headers(client),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["encrypted"] is True
    assert body["value"] == "***"

    # Verify the underlying ciphertext still decrypts to the original.
    from backend.db import SessionLocal
    from backend.services.runtime_config import get_config
    async with SessionLocal() as db:
        plain = await get_config(db, "smtp", "password")
    assert plain == "real-password"


@pytest.mark.asyncio
async def test_put_secret_placeholder_on_empty_row_rejected(client):
    """PUT-ing ``"***"`` to an unset secret returns 400.

    The previous behaviour silently encrypted the literal placeholder
    and reported the row as "configured" — masking the
    mis-configuration behind a green checkmark and surfacing the
    placeholder string itself as the secret value to downstream callers.
    """
    r = await client.put(
        "/api/admin/system-config/oauth/github_client_secret",
        json={"value": "***"},
        headers=_admin_headers(client),
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert "placeholder" in body["detail"].lower()

    # Also confirm no row landed in the DB.
    from backend.db import SessionLocal
    from backend.models import SystemConfig
    async with SessionLocal() as db:
        row = await db.get(
            SystemConfig, ("oauth", "github_client_secret")
        )
    assert row is None


@pytest.mark.asyncio
async def test_delete_falls_back_to_env(client, monkeypatch):
    """DELETE removes the row → reads come back from env."""
    monkeypatch.setenv("ARGUS_GITHUB_CLIENT_ID", "id-from-env")
    # Write a DB override
    r = await client.put(
        "/api/admin/system-config/oauth/github_client_id",
        json={"value": "id-from-db"},
        headers=_admin_headers(client),
    )
    assert r.status_code == 200
    # GET shows DB
    items = (
        await client.get(
            "/api/admin/system-config/oauth", headers=_admin_headers(client)
        )
    ).json()
    by_key = {it["key"]: it for it in items}
    assert by_key["github_client_id"]["value"] == "id-from-db"
    assert by_key["github_client_id"]["source"] == "db"

    # DELETE → falls back to env
    r = await client.delete(
        "/api/admin/system-config/oauth/github_client_id",
        headers=_admin_headers(client),
    )
    assert r.status_code == 204, r.text

    items = (
        await client.get(
            "/api/admin/system-config/oauth", headers=_admin_headers(client)
        )
    ).json()
    by_key = {it["key"]: it for it in items}
    assert by_key["github_client_id"]["value"] == "id-from-env"
    assert by_key["github_client_id"]["source"] == "env"


@pytest.mark.asyncio
async def test_audit_log_records_writes(client):
    """Every write lands in audit_log."""
    r = await client.put(
        "/api/admin/system-config/retention/log_line_days",
        json={"value": 7},
        headers=_admin_headers(client),
    )
    assert r.status_code == 200

    audits = (
        await client.get(
            "/api/admin/audit-log", headers=_admin_headers(client)
        )
    ).json()
    actions = [a["action"] for a in audits]
    assert "system_config_set" in actions


@pytest.mark.asyncio
async def test_unknown_group_returns_404(client):
    r = await client.get(
        "/api/admin/system-config/bogus", headers=_admin_headers(client)
    )
    assert r.status_code == 404
    r = await client.put(
        "/api/admin/system-config/bogus/key",
        json={"value": 1},
        headers=_admin_headers(client),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_feature_flags_through_config_api(client):
    """Feature flags surface alongside other groups via this API."""
    # Default flag bundled with DEFAULT_FLAGS
    r = await client.get(
        "/api/admin/system-config/feature_flags",
        headers=_admin_headers(client),
    )
    assert r.status_code == 200
    items = {it["key"]: it for it in r.json()}
    assert "registration_open" in items
    assert items["registration_open"]["source"] == "default"

    # Toggle it
    r = await client.put(
        "/api/admin/system-config/feature_flags/registration_open",
        json={"value": False},
        headers=_admin_headers(client),
    )
    assert r.status_code == 200
    assert r.json()["value"] is False

    # GET reflects the override
    r = await client.get(
        "/api/admin/system-config/feature_flags",
        headers=_admin_headers(client),
    )
    items = {it["key"]: it for it in r.json()}
    assert items["registration_open"]["value"] is False
    assert items["registration_open"]["source"] == "db"
