"""Read-precedence + encryption tests for ``services.runtime_config``.

The ``client`` fixture is included for its side effect: it boots the
app + drops/creates the test DB schema, which is what these direct-
SessionLocal tests need.  We don't actually issue HTTP calls.
"""
from __future__ import annotations

import pytest

from backend import models  # noqa: F401  - register tables for the in-memory DB
from backend.db import SessionLocal
from backend.services import runtime_config, secrets


@pytest.mark.asyncio
async def test_get_config_falls_back_to_default(client, monkeypatch):
    """No DB row + no env var → return the caller-supplied default."""
    monkeypatch.delenv("ARGUS_GITHUB_CLIENT_ID", raising=False)
    async with SessionLocal() as db:
        v = await runtime_config.get_config(
            db, "oauth", "github_client_id", default="fallback-default"
        )
        assert v == "fallback-default"


@pytest.mark.asyncio
async def test_get_config_reads_env_when_no_db_row(client, monkeypatch):
    """Env var beats default."""
    monkeypatch.setenv("ARGUS_GITHUB_CLIENT_ID", "from-env")
    async with SessionLocal() as db:
        v = await runtime_config.get_config(
            db, "oauth", "github_client_id", default="DEFAULT"
        )
        assert v == "from-env"


@pytest.mark.asyncio
async def test_get_config_db_overrides_env(client, monkeypatch):
    """A ``system_config`` row beats both env and default."""
    monkeypatch.setenv("ARGUS_GITHUB_CLIENT_ID", "from-env")
    async with SessionLocal() as db:
        await runtime_config.set_config(
            db, group="oauth", key="github_client_id", value="from-db"
        )
        await db.commit()
        v = await runtime_config.get_config(
            db, "oauth", "github_client_id", default="x"
        )
        assert v == "from-db"


@pytest.mark.asyncio
async def test_set_config_typed_values_round_trip(client):
    """JSON-typed values come back native (not stringified)."""
    async with SessionLocal() as db:
        await runtime_config.set_config(
            db, group="retention", key="snapshot_days", value=42
        )
        await runtime_config.set_config(
            db, group="smtp", key="use_tls", value=False
        )
        await runtime_config.set_config(
            db, group="oauth", key="github_enabled", value=True
        )
        await db.commit()
        assert await runtime_config.get_config(
            db, "retention", "snapshot_days"
        ) == 42
        assert await runtime_config.get_config(db, "smtp", "use_tls") is False
        assert (
            await runtime_config.get_config(db, "oauth", "github_enabled")
            is True
        )


@pytest.mark.asyncio
async def test_secret_keys_auto_encrypt(client, monkeypatch):
    """Writing a known-secret key auto-flips encrypted=True."""
    # Stable key for deterministic ciphertext across this test
    monkeypatch.setenv("ARGUS_CONFIG_KEY", "test-config-key-stable")
    secrets.reset_for_tests()
    async with SessionLocal() as db:
        row = await runtime_config.set_config(
            db,
            group="oauth",
            key="github_client_secret",
            value="super-secret-value",
        )
        await db.commit()
        assert row.encrypted is True
        # The on-disk value_json is JSON-quoted ciphertext, NOT plaintext.
        assert "super-secret-value" not in row.value_json

        # Round-trip via get_config returns plaintext.
        plain = await runtime_config.get_config(
            db, "oauth", "github_client_secret"
        )
        assert plain == "super-secret-value"
    # Ensure subsequent tests don't inherit our key override.
    secrets.reset_for_tests()


@pytest.mark.asyncio
async def test_delete_config_falls_back_to_env(client, monkeypatch):
    """After DELETE, get_config returns the env value (then default)."""
    monkeypatch.setenv("ARGUS_GITHUB_CLIENT_ID", "from-env")
    async with SessionLocal() as db:
        await runtime_config.set_config(
            db, group="oauth", key="github_client_id", value="from-db"
        )
        await db.commit()
        # Confirm DB wins.
        assert (
            await runtime_config.get_config(
                db, "oauth", "github_client_id"
            )
            == "from-db"
        )
        # Delete → env wins.
        removed = await runtime_config.delete_config(
            db, "oauth", "github_client_id"
        )
        await db.commit()
        assert removed is True
        assert (
            await runtime_config.get_config(
                db, "oauth", "github_client_id", default="x"
            )
            == "from-env"
        )


@pytest.mark.asyncio
async def test_env_value_for_returns_none_for_unmapped():
    assert runtime_config.env_value_for("oauth", "unmapped_key") is None
