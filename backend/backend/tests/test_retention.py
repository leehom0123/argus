"""Tests for the data-retention sweeper.

Covers:
- per-rule deletion (snapshot, event sub-types)
- demo-host snapshot isolation
- batch + job rows are never touched
- admin endpoints (403 / 200 / status)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ago(**kwargs) -> str:
    """Return an ISO timestamp that is ``kwargs`` ago from now."""
    return _iso(_utcnow() - timedelta(**kwargs))


def _fresh() -> str:
    """Return a timestamp 1 minute ago (well within any retention window)."""
    return _ago(minutes=1)


def _settings(**overrides) -> Settings:
    """Build a Settings with all retention caps very short for testing."""
    defaults = {
        "ARGUS_JWT_SECRET": "test-secret-32-bytes-minimum-fixture-value",
        "ARGUS_RETENTION_SNAPSHOT_DAYS": "7",
        "ARGUS_RETENTION_LOG_LINE_DAYS": "14",
        "ARGUS_RETENTION_JOB_EPOCH_DAYS": "30",
        "ARGUS_RETENTION_EVENT_OTHER_DAYS": "90",
        "ARGUS_RETENTION_DEMO_DATA_DAYS": "1",
        "ARGUS_RETENTION_SWEEP_MINUTES": "60",
    }
    defaults.update(overrides)
    import os
    old = {k: os.environ.get(k) for k in defaults}
    for k, v in defaults.items():
        os.environ[k] = v
    try:
        from backend.config import get_settings
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
# Fixture: a raw async DB session (not via the HTTP client)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_session():
    """Yield a fresh async session against the test in-memory DB.

    Reuses the engine that conftest.py already reset/created.
    """
    import backend.db as db_mod
    from backend import models  # noqa: F401

    async with db_mod.engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.drop_all)
        await conn.run_sync(db_mod.Base.metadata.create_all)

    async with db_mod.SessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Unit tests against sweep_once directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_deletes_old_snapshots(db_session):
    """3 old snapshots deleted, 2 fresh ones kept."""
    from backend.models import ResourceSnapshot
    from backend.retention import sweep_once

    old_ts = _ago(days=10)
    fresh_ts = _fresh()

    # 3 old + 2 fresh snapshots on a non-demo host
    rows = [
        ResourceSnapshot(host="gpu-server-1", timestamp=old_ts),
        ResourceSnapshot(host="gpu-server-1", timestamp=old_ts),
        ResourceSnapshot(host="gpu-server-1", timestamp=old_ts),
        ResourceSnapshot(host="gpu-server-1", timestamp=fresh_ts),
        ResourceSnapshot(host="gpu-server-1", timestamp=fresh_ts),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    settings = _settings(ARGUS_RETENTION_SNAPSHOT_DAYS="7")
    stats = await sweep_once(db_session, settings)

    assert stats["resource_snapshot.normal"] == 3


@pytest.mark.asyncio
async def test_sweep_respects_demo_retention_deleted(db_session):
    """Demo snapshot 2 days old is deleted when demo retention is 1 day."""
    from backend.demo.seed import DEMO_HOST
    from backend.models import ResourceSnapshot
    from backend.retention import sweep_once

    db_session.add(ResourceSnapshot(host=DEMO_HOST, timestamp=_ago(days=2)))
    await db_session.commit()

    settings = _settings(
        ARGUS_RETENTION_DEMO_DATA_DAYS="1",
        ARGUS_RETENTION_SNAPSHOT_DAYS="7",
    )
    stats = await sweep_once(db_session, settings)

    assert stats["resource_snapshot.demo"] == 1


@pytest.mark.asyncio
async def test_sweep_respects_demo_retention_kept(db_session):
    """Demo snapshot 1 hour old survives when demo retention is 1 day."""
    from backend.demo.seed import DEMO_HOST
    from backend.models import ResourceSnapshot
    from backend.retention import sweep_once

    db_session.add(ResourceSnapshot(host=DEMO_HOST, timestamp=_ago(hours=1)))
    await db_session.commit()

    settings = _settings(
        ARGUS_RETENTION_DEMO_DATA_DAYS="1",
        ARGUS_RETENTION_SNAPSHOT_DAYS="7",
    )
    stats = await sweep_once(db_session, settings)

    assert stats.get("resource_snapshot.demo", 0) == 0


@pytest.mark.asyncio
async def test_sweep_event_type_splits(db_session):
    """Per-rule retention splits work: log_line, job_epoch, and other types
    are each governed by their own cap."""
    from backend.models import Event
    from backend.retention import sweep_once

    batch_id = "retention-test-batch"

    def _ev(event_type: str, ts: str) -> Event:
        return Event(
            batch_id=batch_id,
            event_type=event_type,
            timestamp=ts,
            schema_version="1.1",
            event_id=str(uuid.uuid4()),
        )

    # 3 log_line events: 2 old (>14d), 1 fresh
    # 3 job_epoch events: 2 old (>30d), 1 fresh
    # 4 other events:    2 old (>90d), 2 fresh
    events = [
        # log_line
        _ev("log_line", _ago(days=20)),
        _ev("log_line", _ago(days=15)),
        _ev("log_line", _fresh()),
        # job_epoch
        _ev("job_epoch", _ago(days=40)),
        _ev("job_epoch", _ago(days=31)),
        _ev("job_epoch", _fresh()),
        # other (batch_start, job_done, …)
        _ev("batch_start", _ago(days=100)),
        _ev("batch_done", _ago(days=95)),
        _ev("batch_start", _fresh()),
        _ev("job_done", _fresh()),
    ]
    db_session.add_all(events)
    await db_session.commit()

    settings = _settings(
        ARGUS_RETENTION_LOG_LINE_DAYS="14",
        ARGUS_RETENTION_JOB_EPOCH_DAYS="30",
        ARGUS_RETENTION_EVENT_OTHER_DAYS="90",
    )
    stats = await sweep_once(db_session, settings)

    assert stats["event.log_line"] == 2, stats
    assert stats["event.job_epoch"] == 2, stats
    assert stats["event.other"] == 2, stats


@pytest.mark.asyncio
async def test_sweep_does_not_delete_batch_or_job_rows(db_session):
    """Batch and Job rows are never touched by sweep_once."""
    from backend.models import Batch, Job
    from backend.retention import sweep_once

    # Insert a batch and a job with old timestamps in their string fields.
    batch = Batch(
        id="archive-batch-001",
        project="archive-project",
        host="archive-host",
        start_time=_ago(days=365),
        end_time=_ago(days=364),
        is_deleted=False,
    )
    db_session.add(batch)
    await db_session.flush()

    job = Job(
        id="archive-job-001",
        batch_id="archive-batch-001",
        start_time=_ago(days=365),
        end_time=_ago(days=364),
    )
    db_session.add(job)
    await db_session.commit()

    # Run sweep with very aggressive retention (1 day for everything).
    settings = _settings(
        ARGUS_RETENTION_SNAPSHOT_DAYS="1",
        ARGUS_RETENTION_LOG_LINE_DAYS="1",
        ARGUS_RETENTION_JOB_EPOCH_DAYS="1",
        ARGUS_RETENTION_EVENT_OTHER_DAYS="1",
        ARGUS_RETENTION_DEMO_DATA_DAYS="1",
    )
    await sweep_once(db_session, settings)

    # Batch and Job rows must still exist.
    from sqlalchemy import select
    import backend.db as db_mod

    async with db_mod.SessionLocal() as check_session:
        b = await check_session.get(Batch, "archive-batch-001")
        assert b is not None, "Batch row was unexpectedly deleted"
        j = (
            await check_session.execute(
                select(Job).where(Job.id == "archive-job-001")
            )
        ).scalars().first()
        assert j is not None, "Job row was unexpectedly deleted"


# ---------------------------------------------------------------------------
# Admin endpoint tests (via HTTP client)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_sweep_endpoint_403_for_non_admin(client):
    """Non-admin user gets 403 when hitting POST /api/admin/retention/sweep."""
    # Register a second (non-admin) user and grab their JWT.
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": "notadmin",
            "email": "notadmin@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": "notadmin", "password": "password123"},
    )
    jwt = lr.json()["access_token"]

    r = await client.post(
        "/api/admin/retention/sweep",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_sweep_endpoint_200_for_admin(client):
    """Admin user gets 200 and a stats dict from POST /api/admin/retention/sweep."""
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/admin/retention/sweep",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "stats" in body
    assert "elapsed_ms" in body
    assert isinstance(body["elapsed_ms"], int)
    assert body["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_admin_status_endpoint(client):
    """GET /api/admin/retention/status returns settings and run info."""
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # First call: no sweep run yet in this session → last_run_at is None.
    r = await client.get(
        "/api/admin/retention/status",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "settings" in body
    s = body["settings"]
    assert "retention_snapshot_days" in s
    assert "retention_log_line_days" in s
    assert "retention_job_epoch_days" in s
    assert "retention_event_other_days" in s
    assert "retention_demo_data_days" in s
    assert "retention_sweep_interval_minutes" in s
    # next_run_at is None because no sweep has run yet
    # (the module-level dict may carry state from another test, but that
    # is fine — we only assert the shape, not a specific value).
    assert "last_run_at" in body
    assert "next_run_at" in body

    # Trigger a sweep so last_run_at gets populated.
    r2 = await client.post(
        "/api/admin/retention/sweep",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r2.status_code == 200

    r3 = await client.get(
        "/api/admin/retention/status",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3["last_run_at"] is not None
    assert body3["last_run_stats"] is not None
