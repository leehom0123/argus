"""N1 fix smoke check: ``warn_if_using_jwt_fallback`` logs at startup
when encrypted ``system_config`` rows exist but ``ARGUS_CONFIG_KEY`` is
unset.

Operators who rotate ``ARGUS_JWT_SECRET`` without first setting
``ARGUS_CONFIG_KEY`` lose every encrypted config value (the Fernet key
derives from the JWT secret in the fallback path). The startup warning
makes that coupling visible *before* the rotation event causes the
loss.
"""
from __future__ import annotations

import logging

import pytest

from backend.db import SessionLocal
from backend.services import runtime_config, secrets


@pytest.mark.asyncio
async def test_warn_if_using_jwt_fallback_logs(client, monkeypatch, caplog):
    """An encrypted row + no ARGUS_CONFIG_KEY → a single warning."""
    monkeypatch.delenv("ARGUS_CONFIG_KEY", raising=False)

    # Plant an encrypted row.
    async with SessionLocal() as db:
        await runtime_config.set_config(
            db, group="smtp", key="password", value="dummy-pw"
        )
        await db.commit()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=secrets.log.name):
        await secrets.warn_if_using_jwt_fallback()

    matched = [
        rec for rec in caplog.records
        if "ARGUS_CONFIG_KEY unset" in rec.getMessage()
    ]
    assert len(matched) == 1, [rec.getMessage() for rec in caplog.records]
    assert "JWT secret fallback" in matched[0].getMessage()


@pytest.mark.asyncio
async def test_warn_skipped_when_config_key_set(client, monkeypatch, caplog):
    """No warning when ARGUS_CONFIG_KEY is set, even with encrypted rows."""
    monkeypatch.setenv("ARGUS_CONFIG_KEY", "an-explicit-operator-config-key")
    secrets.reset_for_tests()

    async with SessionLocal() as db:
        await runtime_config.set_config(
            db, group="smtp", key="password", value="dummy-pw"
        )
        await db.commit()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=secrets.log.name):
        await secrets.warn_if_using_jwt_fallback()

    assert not [
        rec for rec in caplog.records
        if "ARGUS_CONFIG_KEY unset" in rec.getMessage()
    ]
    secrets.reset_for_tests()


@pytest.mark.asyncio
async def test_warn_skipped_when_no_encrypted_rows(
    client, monkeypatch, caplog
):
    """No warning on a fresh DB with no encrypted rows yet."""
    monkeypatch.delenv("ARGUS_CONFIG_KEY", raising=False)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=secrets.log.name):
        await secrets.warn_if_using_jwt_fallback()

    assert not [
        rec for rec in caplog.records
        if "ARGUS_CONFIG_KEY unset" in rec.getMessage()
    ]
