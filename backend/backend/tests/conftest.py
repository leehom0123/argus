"""Shared pytest fixtures for the backend tests.

We install a single process-wide async engine that points at an in-memory
SQLite (shared via StaticPool so all sessions see the same DB). Between tests
we drop and recreate every table so each test starts clean.

The ``client`` fixture is **pre-authenticated**: it registers a test user,
mints a reporter-scope API token, and attaches ``Authorization: Bearer
em_live_…`` headers by default. This keeps the original 52 tests (written
before BACKEND-B's auth wiring landed) passing: they still hit
``POST /api/events`` and the default token carries them through. Tests
that need to exercise the unauth surface (``test_me_requires_auth`` etc.)
just strip or override the header on the ``client`` instance.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio


def _now_iso() -> str:
    """Wall-clock UTC ISO timestamp (``YYYY-MM-DDTHH:MM:SSZ``).

    Use in fixtures that feed recency-filtered code paths so the seeded
    event always lands inside the production cutoff window. The known
    cutoffs in the codebase are:

    * Dashboard host cards (``DashboardService._host_summary`` /
      ``_host_cards``) — last 5 minutes.
    * Admin host bulk-delete (``api/admin.py::bulk_delete_hosts``) —
      last 10 minutes (active-host guard).
    * Dashboard 24h job counters (``cutoff_24h`` in ``_overview``).
    * Project ``batches_this_week`` rollup — last 7 days.

    A hardcoded ``"2026-04-25T..."`` string falls outside the 5/10-min
    window the moment the suite runs on a later day, silently dropping
    the seeded row out of the response. This helper anchors fixtures to
    wall-clock instead.

    Pair with :func:`_recent_iso` when the test needs a *sequence* of
    events with relative ordering (``snapshot_t-30s``, ``snapshot_t-0s``).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recent_iso(seconds_ago: int = 0) -> str:
    """Wall-clock minus ``seconds_ago`` seconds, ISO UTC.

    Use when a test seeds multiple events that need to preserve relative
    ordering — e.g. three snapshots 10 s apart. Pass the *oldest* offset
    first and shrink toward 0 to keep the natural "earlier → later"
    direction:

    .. code-block:: python

        _recent_iso(60)  # earliest event
        _recent_iso(30)
        _recent_iso(0)   # latest event

    All three land inside the dashboard's 5-minute host-card cutoff
    regardless of the calendar date the suite runs on, while preserving
    the 60 s span the test relies on.
    """
    delta = timedelta(seconds=seconds_ago)
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _install_test_env() -> None:
    # Point all downstream imports at an in-memory URL. StaticPool is applied
    # in backend.db when the URL is sqlite ``:memory:``.
    os.environ["ARGUS_DB_URL"] = "sqlite+aiosqlite:///:memory:"
    # Stable secret so tests can decode tokens deterministically.
    os.environ.setdefault(
        "ARGUS_JWT_SECRET",
        "test-secret-32-bytes-minimum-fixture-value",
    )
    # Leave SMTP unconfigured so EmailService uses the stdout fallback.
    os.environ.setdefault("ARGUS_BASE_URL", "http://localhost:5173")
    # Skip the built-in demo seeder at test startup so the ~300 existing
    # tests keep assuming an empty DB. ``test_demo_seed`` flips this
    # back off for its dedicated lifespan fixture.
    os.environ.setdefault("ARGUS_SKIP_DEMO_SEED", "1")
    # Disable the watchdog + retention loops so their background tasks
    # don't try to run against the in-memory DB after teardown.
    os.environ.setdefault("ARGUS_WATCHDOG_ENABLED", "false")
    os.environ.setdefault("ARGUS_RETENTION_SWEEP_MINUTES", "0")
    # Ditto for the guardrails SQLite backup cron — in-memory DBs can't
    # be .backup()'d, and real-file tests pass a non-zero override.
    os.environ.setdefault("ARGUS_BACKUP_INTERVAL_H", "0")
    # Silence the anomalous-login email during the default suite —
    # ``test_anomalous_login.py`` flips this on via monkeypatch.
    os.environ.setdefault("ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED", "false")
    # Team Email: default-skip the email template seeder
    os.environ.setdefault("ARGUS_SKIP_EMAIL_SEED", "1")
    # Team Email: don't start the async email worker by default
    os.environ.setdefault("ARGUS_EMAIL_WORKER_ENABLED", "false")


_install_test_env()


@pytest_asyncio.fixture
async def client() -> AsyncIterator:
    """Yield a pre-authenticated httpx AsyncClient.

    The fixture:
      1. Resets the in-memory DB + per-process auth state
      2. Registers ``tester`` as the first user (→ admin)
      3. Logs in to obtain a JWT
      4. Creates a reporter-scope API token via the JWT-authenticated
         ``/api/tokens`` endpoint
      5. Sets ``Authorization: Bearer <em_live_...>`` as a default
         header so subsequent POST /api/events calls authenticate
    """
    from httpx import ASGITransport, AsyncClient

    import backend.db as db_mod
    from backend import models  # noqa: F401 - ensure models registered
    from backend.app import create_app
    from backend.auth.jwt import clear_blacklist_for_tests
    from backend.config import get_settings
    from backend.services.email import reset_email_service_for_tests
    from backend.services.jwt_rotation import reset_cache_for_tests as _jwt_rotation_reset
    from backend.utils.ratelimit import (
        reset_change_email_bucket_for_tests,
        reset_change_password_bucket_for_tests,
        reset_default_bucket_for_tests,
        reset_public_bucket_for_tests,
        reset_resend_verification_bucket_for_tests,
        reset_smtp_test_bucket_for_tests,
    )
    from backend.utils.response_cache import default_cache as _response_cache

    # Purge caches before each test so env overrides take effect.
    get_settings.cache_clear()
    reset_email_service_for_tests()
    clear_blacklist_for_tests()
    _jwt_rotation_reset()
    reset_default_bucket_for_tests()
    reset_public_bucket_for_tests()
    reset_change_password_bucket_for_tests()
    reset_change_email_bucket_for_tests()
    reset_smtp_test_bucket_for_tests()
    reset_resend_verification_bucket_for_tests()
    # TTL response cache is module-level, so stale entries from the
    # previous test would bleed into this one and return empty
    # payloads for freshly-seeded data.
    _response_cache.clear()

    # Reset schema between tests. Using metadata.drop_all + create_all keeps
    # the engine identity stable so sessions opened via get_session still use
    # the same in-memory DB.
    async with db_mod.engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.drop_all)
        await conn.run_sync(db_mod.Base.metadata.create_all)

    app = create_app()
    # Run lifespan so notification state is configured, though startup
    # init_db here is a no-op since we just created the tables.
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            # Provision a default user + reporter token so legacy tests
            # (written before auth existed) keep working.
            reg = await ac.post(
                "/api/auth/register",
                json={
                    "username": "tester",
                    "email": "tester@example.com",
                    "password": "password123",
                },
            )
            assert reg.status_code == 201, reg.text

            login = await ac.post(
                "/api/auth/login",
                json={
                    "username_or_email": "tester",
                    "password": "password123",
                },
            )
            assert login.status_code == 200, login.text
            jwt = login.json()["access_token"]

            tok_resp = await ac.post(
                "/api/tokens",
                json={"name": "default-test-token", "scope": "reporter"},
                headers={"Authorization": f"Bearer {jwt}"},
            )
            assert tok_resp.status_code == 201, tok_resp.text
            api_token = tok_resp.json()["token"]

            # Default every subsequent request to the reporter token so
            # POST /api/events works without per-test plumbing. Tests
            # needing to exercise unauthed paths can do
            # ``client.headers.pop("Authorization")``.
            ac.headers.update({"Authorization": f"Bearer {api_token}"})
            # Stash convenience fields on the client so tests that DO
            # want to introspect the default auth can read them.
            ac._test_default_jwt = jwt  # type: ignore[attr-defined]
            ac._test_default_token = api_token  # type: ignore[attr-defined]
            ac._test_default_username = "tester"  # type: ignore[attr-defined]
            yield ac


@pytest_asyncio.fixture
async def unauthed_client() -> AsyncIterator:
    """A bare AsyncClient with no default Authorization header.

    Useful for tests that specifically verify the 401 contract (the
    legacy ``test_me_requires_auth`` uses its own construction, but
    new tests can just pull this fixture).
    """
    from httpx import ASGITransport, AsyncClient

    import backend.db as db_mod
    from backend.app import create_app
    from backend.auth.jwt import clear_blacklist_for_tests
    from backend.config import get_settings
    from backend.services.email import reset_email_service_for_tests
    from backend.services.jwt_rotation import reset_cache_for_tests as _jwt_rotation_reset
    from backend.utils.ratelimit import (
        reset_change_email_bucket_for_tests,
        reset_change_password_bucket_for_tests,
        reset_default_bucket_for_tests,
        reset_public_bucket_for_tests,
        reset_resend_verification_bucket_for_tests,
        reset_smtp_test_bucket_for_tests,
    )
    from backend.utils.response_cache import default_cache as _response_cache

    get_settings.cache_clear()
    reset_email_service_for_tests()
    clear_blacklist_for_tests()
    _jwt_rotation_reset()
    reset_default_bucket_for_tests()
    reset_public_bucket_for_tests()
    reset_change_password_bucket_for_tests()
    reset_change_email_bucket_for_tests()
    reset_smtp_test_bucket_for_tests()
    reset_resend_verification_bucket_for_tests()
    _response_cache.clear()

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


@pytest_asyncio.fixture
async def email_service():
    """Return the process-wide :class:`EmailService` with a fresh outbox.

    We reset the singleton in the ``client`` fixture already; this just
    exposes a handle for tests that want to assert on `.sent_messages`.
    """
    from backend.services.email import get_email_service

    svc = get_email_service()
    svc.sent_messages.clear()
    return svc


@pytest.fixture
def sample_events() -> list[dict]:
    """One consistent sequence of valid events covering every event_type.

    All events carry ``schema_version="1.1"`` and a unique UUID
    ``event_id`` — the legacy v1.0 wire format is no longer accepted by
    the backend (Phase-3 post-review M2).

    Timestamps are anchored to wall-clock ``datetime.now(timezone.utc)``
    via :func:`_recent_iso` so the sequence stays inside the dashboard's
    5-minute host-card cutoff and the 24-hour job-counter cutoff. The
    relative spacing matches the original hardcoded values:

    * ``batch_start``        — t-120 s
    * ``job_start``          —  t-60 s
    * ``resource_snapshot``  —  t-55 s  (interleaved between start + epoch 1)
    * ``job_epoch`` (1)      —  t-50 s
    * ``job_epoch`` (2)      —  t-40 s
    * ``job_done``           —  t-30 s
    * ``batch_done``         —    t-0 s
    """
    batch_id = "bench-test-1"
    job_id = "etth1_transformer"
    src = {"project": "test", "host": "localhost", "user": "u"}
    return [
        {
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "batch_start",
            "timestamp": _recent_iso(seconds_ago=120),
            "batch_id": batch_id,
            "source": src,
            "data": {"experiment_type": "forecast", "n_total_jobs": 12,
                     "command": "run_benchmark.py"},
        },
        {
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_start",
            "timestamp": _recent_iso(seconds_ago=60),
            "batch_id": batch_id,
            "job_id": job_id,
            "source": src,
            "data": {"model": "transformer", "dataset": "etth1"},
        },
        {
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_epoch",
            "timestamp": _recent_iso(seconds_ago=50),
            "batch_id": batch_id,
            "job_id": job_id,
            "source": src,
            "data": {"epoch": 1, "train_loss": 0.42, "val_loss": 0.47,
                     "lr": 1e-4},
        },
        {
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_epoch",
            "timestamp": _recent_iso(seconds_ago=40),
            "batch_id": batch_id,
            "job_id": job_id,
            "source": src,
            "data": {"epoch": 2, "train_loss": 0.30, "val_loss": 0.33,
                     "lr": 1e-4},
        },
        {
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_done",
            "timestamp": _recent_iso(seconds_ago=30),
            "batch_id": batch_id,
            "job_id": job_id,
            "source": src,
            "data": {"status": "DONE", "elapsed_s": 30, "train_epochs": 2,
                     "metrics": {"MSE": 0.25, "MAE": 0.31}},
        },
        {
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "resource_snapshot",
            "timestamp": _recent_iso(seconds_ago=55),
            "batch_id": batch_id,
            "source": src,
            "data": {"gpu_util_pct": 80, "gpu_mem_mb": 2000,
                     "gpu_mem_total_mb": 24000, "cpu_util_pct": 50,
                     "ram_mb": 4000, "ram_total_mb": 64000},
        },
        {
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "batch_done",
            "timestamp": _recent_iso(seconds_ago=0),
            "batch_id": batch_id,
            "source": src,
            "data": {"n_done": 1, "n_failed": 0, "total_elapsed_s": 120},
        },
    ]
