"""Tests for the built-in demo project fixture.

Covers the seeder (``backend.demo.seed.seed_demo``), the admin reset
endpoint, the hide_demo preference + user-preferences endpoint, and
the startup-hook path that plants the fixture on a fresh DB.

We re-use the shared ``client`` fixture (pre-authenticated as the
first user → admin) because the admin endpoint needs an admin caller
anyway. Tests that need a non-admin user mint one via the auth
register endpoint like the rest of the suite.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.db import SessionLocal
from backend.demo import DEMO_BATCH_ID, DEMO_PROJECT, seed_demo
from backend.models import Batch, Event, Job, ProjectMeta, ResourceSnapshot, User


async def _mk_user(client, username: str) -> tuple[str, int]:
    """Register a non-admin user and return ``(jwt, user_id)``."""
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    user_id = reg.json()["user_id"]
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
    )
    assert lr.status_code == 200, lr.text
    return lr.json()["access_token"], user_id


async def _count_demo_projects() -> int:
    """Return how many ProjectMeta rows match the demo key."""
    async with SessionLocal() as db:
        row = await db.execute(
            select(ProjectMeta).where(ProjectMeta.project == DEMO_PROJECT)
        )
        return len(row.scalars().all())


# ---------------------------------------------------------------------------
# Seeder idempotency + force
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_idempotent(client):
    """Calling seed_demo twice leaves exactly one demo project.

    The ``client`` fixture may or may not have invoked the startup
    seeder depending on the ``ARGUS_SKIP_DEMO_SEED`` env default in
    the surrounding test environment. We establish a clean starting
    state by wiping any prior demo rows before the idempotency check,
    so the assertion captures just the behaviour under test.
    """
    # Normalise starting state.
    async with SessionLocal() as db:
        await seed_demo(db, force=True)

    async with SessionLocal() as db:
        first = await seed_demo(db)  # already seeded → no-op
    async with SessionLocal() as db:
        second = await seed_demo(db)
    assert first is False  # existing row short-circuits both calls
    assert second is False
    assert await _count_demo_projects() == 1

    # Verify the batch + jobs + snapshots counts match the spec.
    async with SessionLocal() as db:
        batches = (
            await db.execute(
                select(Batch).where(Batch.id == DEMO_BATCH_ID)
            )
        ).scalars().all()
        assert len(batches) == 1
        jobs = (
            await db.execute(
                select(Job).where(Job.batch_id == DEMO_BATCH_ID)
            )
        ).scalars().all()
        assert len(jobs) == 60
        statuses = [j.status for j in jobs]
        assert statuses.count("done") == 48
        assert statuses.count("running") == 10
        assert statuses.count("failed") == 2
        snaps = (
            await db.execute(
                select(ResourceSnapshot).where(
                    ResourceSnapshot.batch_id == DEMO_BATCH_ID
                )
            )
        ).scalars().all()
        assert len(snaps) == 100


@pytest.mark.asyncio
async def test_seed_force_resets(client):
    """force=True wipes and re-seeds — same row count, new published_at."""
    async with SessionLocal() as db:
        await seed_demo(db)
    async with SessionLocal() as db:
        first_meta = await db.get(ProjectMeta, DEMO_PROJECT)
        first_published = first_meta.published_at

    # Manually mutate a field to prove force=True replaces it.
    async with SessionLocal() as db:
        batch = await db.get(Batch, DEMO_BATCH_ID)
        batch.status = "corrupted"
        await db.commit()

    async with SessionLocal() as db:
        result = await seed_demo(db, force=True)
    assert result is True

    # One demo project, batch status restored.
    assert await _count_demo_projects() == 1
    async with SessionLocal() as db:
        batch = await db.get(Batch, DEMO_BATCH_ID)
        assert batch.status == "running"
        # published_at should also refresh (same second on fast test runs
        # is fine — we just check it's not None).
        new_meta = await db.get(ProjectMeta, DEMO_PROJECT)
        assert new_meta.published_at is not None
        assert new_meta.published_at >= first_published


# ---------------------------------------------------------------------------
# hide_demo preference behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hide_demo_toggle_is_deprecated_noop(client):
    """Flipping ``hide_demo`` on either setting has no effect: demo is
    unconditionally hidden from authenticated users since 2026-04-24.

    The column is preserved for backwards compatibility with older
    clients that still PATCH it, but it no longer drives any filter.
    """
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt, user_id = await _mk_user(client, "alice")

    # hide_demo=True → demo still absent (as it should be).
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        user.hide_demo = True
        await db.commit()
    r = await client.get(
        "/api/projects",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text
    assert DEMO_PROJECT not in {row["project"] for row in r.json()}

    # hide_demo=False → demo STILL absent (new rule — no opt-in).
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        user.hide_demo = False
        await db.commit()
    r = await client.get(
        "/api/projects",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200
    assert DEMO_PROJECT not in {row["project"] for row in r.json()}


@pytest.mark.asyncio
async def test_demo_direct_access_is_404_for_authenticated(client):
    """Logged-in users get 404 when navigating to the demo project
    directly — the fixture is reachable only through the anonymous
    ``/api/public/projects`` surface.
    """
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt, _ = await _mk_user(client, "bob")
    r = await client.get(
        f"/api/projects/{DEMO_PROJECT}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# /api/users/me/preferences round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preferences_round_trip(client):
    """PATCH hide_demo=True → /me + /preferences both reflect it."""
    jwt, _ = await _mk_user(client, "carol")
    headers = {"Authorization": f"Bearer {jwt}"}

    # Baseline.
    r = await client.get("/api/users/me/preferences", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"hide_demo": False, "preferred_locale": "en-US"}

    # PATCH on.
    r = await client.patch(
        "/api/users/me/preferences",
        headers=headers,
        json={"hide_demo": True},
    )
    assert r.status_code == 200
    assert r.json()["hide_demo"] is True

    # /auth/me now surfaces the same value.
    me = await client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["hide_demo"] is True

    # PATCH with empty body is a no-op but still 200.
    r = await client.patch(
        "/api/users/me/preferences", headers=headers, json={}
    )
    assert r.status_code == 200
    assert r.json()["hide_demo"] is True

    # Patch locale.
    r = await client.patch(
        "/api/users/me/preferences",
        headers=headers,
        json={"preferred_locale": "zh-CN"},
    )
    assert r.status_code == 200
    assert r.json()["preferred_locale"] == "zh-CN"
    assert r.json()["hide_demo"] is True


# ---------------------------------------------------------------------------
# Admin reset endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_reset_demo_403_for_non_admin(client):
    """Non-admin hitting /api/admin/demo/reset gets 403."""
    jwt, _ = await _mk_user(client, "dan")
    r = await client.post(
        "/api/admin/demo/reset",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_reset_demo_204_for_admin(client):
    """Admin hitting /api/admin/demo/reset gets 204 and the demo exists."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.post(
        "/api/admin/demo/reset",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 204, r.text
    # After reset there must be exactly one demo project regardless of
    # whether the startup seeder had already run.
    assert await _count_demo_projects() == 1


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeding_client():
    """A client whose lifespan actually runs the demo seeder.

    Mirrors the shared ``client`` fixture but flips
    ``ARGUS_SKIP_DEMO_SEED`` off for the duration of one test so
    the lifespan call to ``seed_demo()`` populates the DB the same
    way a production startup would.
    """
    from httpx import ASGITransport, AsyncClient

    import backend.db as db_mod
    from backend.app import create_app
    from backend.auth.jwt import clear_blacklist_for_tests
    from backend.config import get_settings
    from backend.services.email import reset_email_service_for_tests
    from backend.utils.ratelimit import (
        reset_default_bucket_for_tests,
        reset_public_bucket_for_tests,
    )

    get_settings.cache_clear()
    reset_email_service_for_tests()
    clear_blacklist_for_tests()
    reset_default_bucket_for_tests()
    reset_public_bucket_for_tests()

    async with db_mod.engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.drop_all)
        await conn.run_sync(db_mod.Base.metadata.create_all)

    previous = os.environ.get("ARGUS_SKIP_DEMO_SEED")
    os.environ.pop("ARGUS_SKIP_DEMO_SEED", None)
    try:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                yield ac
    finally:
        if previous is not None:
            os.environ["ARGUS_SKIP_DEMO_SEED"] = previous


@pytest.mark.asyncio
async def test_startup_hook_seeds_empty_db(seeding_client):
    """A fresh DB booted through lifespan ends up with the demo seeded."""
    assert await _count_demo_projects() == 1

    # Public list reachable without auth (A-方案 endpoints pick up
    # ``is_public=True`` demo rows; we only assert the underlying DB
    # state here because the list endpoint itself is out of our scope).
    async with SessionLocal() as db:
        meta = await db.get(ProjectMeta, DEMO_PROJECT)
        assert meta is not None
        assert meta.is_demo is True
        assert meta.is_public is True
