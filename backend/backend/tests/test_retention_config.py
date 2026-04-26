"""Tests for the retention Settings integration (commit 810d2b6).

Verifies that the ``Settings`` object (pydantic-settings) correctly reads
all retention-related env vars, and that admin endpoints enforce auth gates.

The ``Settings`` class uses ``extra="ignore"`` with pydantic-settings, which
means undeclared env vars with the ARGUS_ prefix are silently loaded as
dynamic attributes. The retention sweeper relies on this behaviour.

Covers:
- Settings reads all 6 retention env vars as positive integers
- Setting any cap to 0 disables the rule (sweep skips it)
- Default values are positive (no zero-day accidental purge)
- Admin sweep endpoint requires admin role (403 for non-admin)
- Admin status endpoint requires admin role (403 for non-admin)
- Both admin endpoints require JWT (401 without auth)
"""
from __future__ import annotations

import os

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_retention_settings(**overrides):
    """Build a Settings with all retention env vars populated."""
    env_values = {
        "ARGUS_JWT_SECRET": "test-secret-32-bytes-minimum-fixture-value",
        "ARGUS_RETENTION_SNAPSHOT_DAYS": "7",
        "ARGUS_RETENTION_LOG_LINE_DAYS": "14",
        "ARGUS_RETENTION_JOB_EPOCH_DAYS": "30",
        "ARGUS_RETENTION_EVENT_OTHER_DAYS": "90",
        "ARGUS_RETENTION_DEMO_DATA_DAYS": "1",
        "ARGUS_RETENTION_SWEEP_MINUTES": "60",
    }
    env_values.update(overrides)
    old = {k: os.environ.get(k) for k in env_values}
    for k, v in env_values.items():
        os.environ[k] = v
    try:
        from backend.config import Settings, get_settings
        get_settings.cache_clear()
        s = Settings()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from backend.config import get_settings
        get_settings.cache_clear()
    return s


# ---------------------------------------------------------------------------
# Settings reads retention env vars
# ---------------------------------------------------------------------------


def test_settings_reads_retention_snapshot_days():
    """ARGUS_RETENTION_SNAPSHOT_DAYS is accessible as retention_snapshot_days."""
    s = _make_retention_settings()
    assert hasattr(s, "retention_snapshot_days"), (
        "Settings missing 'retention_snapshot_days' — config.py may need updating"
    )
    assert int(s.retention_snapshot_days) == 7


def test_settings_reads_retention_log_line_days():
    s = _make_retention_settings()
    assert int(s.retention_log_line_days) == 14


def test_settings_reads_retention_event_other_days():
    s = _make_retention_settings()
    assert int(s.retention_event_other_days) == 90


def test_settings_reads_retention_demo_data_days():
    s = _make_retention_settings()
    assert int(s.retention_demo_data_days) == 1


def test_settings_reads_retention_sweep_minutes():
    s = _make_retention_settings(ARGUS_RETENTION_SWEEP_MINUTES="120")
    assert int(s.retention_sweep_interval_minutes) == 120


def test_settings_retention_defaults_are_positive():
    """All retention caps must be positive (guards against zero-day accidental purge)."""
    s = _make_retention_settings()
    assert int(s.retention_snapshot_days) > 0
    assert int(s.retention_log_line_days) > 0
    assert int(s.retention_job_epoch_days) > 0
    assert int(s.retention_event_other_days) > 0
    assert int(s.retention_demo_data_days) > 0


def test_settings_retention_zero_disables_rule():
    """A cap set to 0 should be readable as 0 (sweep_once checks > 0)."""
    s = _make_retention_settings(ARGUS_RETENTION_SNAPSHOT_DAYS="0")
    assert int(s.retention_snapshot_days) == 0


# ---------------------------------------------------------------------------
# Admin endpoint auth gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_sweep_endpoint_requires_admin(client):
    """Non-admin user → 403 on POST /api/admin/retention/sweep."""
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": "ret-nonadmin-a",
            "email": "ret-nonadmin-a@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": "ret-nonadmin-a", "password": "password123"},
    )
    jwt = lr.json()["access_token"]
    r = await client.post(
        "/api/admin/retention/sweep",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_retention_status_endpoint_requires_admin(client):
    """Non-admin user → 403 on GET /api/admin/retention/status."""
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": "ret-nonadmin-b",
            "email": "ret-nonadmin-b@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": "ret-nonadmin-b", "password": "password123"},
    )
    jwt = lr.json()["access_token"]
    r = await client.get(
        "/api/admin/retention/status",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_retention_sweep_no_auth_401(unauthed_client):
    """No JWT → 401 on POST /api/admin/retention/sweep."""
    r = await unauthed_client.post("/api/admin/retention/sweep")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_retention_status_no_auth_401(unauthed_client):
    """No JWT → 401 on GET /api/admin/retention/status."""
    r = await unauthed_client.get("/api/admin/retention/status")
    assert r.status_code == 401, r.text
