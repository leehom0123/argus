"""Tests for ``GET /api/stats/gpu-hours-by-user`` (roadmap #11).

Covers:
  1. Anonymous request → 401
  2. Single-user happy path — aggregation sums elapsed_s * gpu_count / 3600
  3. ``days`` query param caps the window (older jobs excluded)
  4. Admin sees every user; non-admin sees only self
  5. Non-admin with zero jobs still gets exactly one row (stable shape)
  6. ``days`` out-of-range → 422 validation error
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed_job(
    session, batch_id: str, job_id: str, owner_id: int,
    elapsed_s: int, end_time: datetime, gpu_count: int | None = None,
) -> None:
    """Insert a ``Batch`` + ``Job`` pair owned by ``owner_id``."""
    from backend.models import Batch, Job

    # upsert batch
    existing = await session.get(Batch, batch_id)
    if existing is None:
        session.add(Batch(
            id=batch_id,
            project="p",
            owner_id=owner_id,
            status="done",
            start_time=_iso(end_time - timedelta(seconds=elapsed_s)),
            end_time=_iso(end_time),
            n_done=1,
            n_failed=0,
        ))
    metrics = None
    if gpu_count is not None:
        metrics = json.dumps({"MSE": 0.1, "gpu_count": gpu_count})
    session.add(Job(
        id=job_id,
        batch_id=batch_id,
        model="transformer",
        dataset="etth1",
        status="done",
        start_time=_iso(end_time - timedelta(seconds=elapsed_s)),
        end_time=_iso(end_time),
        elapsed_s=elapsed_s,
        metrics=metrics,
    ))
    await session.commit()


@pytest.mark.asyncio
async def test_gpu_hours_requires_auth(unauthed_client):
    """No token → 401."""
    r = await unauthed_client.get("/api/stats/gpu-hours-by-user")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_gpu_hours_single_user_aggregates(client):
    """Single job: 3600s at gpu_count=2 → 2.0 gpu_hours."""
    import backend.db as db_mod
    from backend.models import User

    async with db_mod.SessionLocal() as session:
        tester = (
            await session.execute(
                select(User).where(User.username == "tester")
            )
        ).scalar_one()
        await _seed_job(
            session, "b-ghours-1", "j-1",
            owner_id=tester.id,
            elapsed_s=3600,
            end_time=datetime.now(timezone.utc) - timedelta(hours=1),
            gpu_count=2,
        )

    r = await client.get("/api/stats/gpu-hours-by-user")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["username"] == "tester"
    assert row["job_count"] == 1
    assert row["gpu_hours"] == pytest.approx(2.0, rel=1e-3)


@pytest.mark.asyncio
async def test_gpu_hours_missing_gpu_count_defaults_to_one(client):
    """Metrics without gpu_count → treated as 1 GPU."""
    import backend.db as db_mod
    from backend.models import User

    async with db_mod.SessionLocal() as session:
        tester = (
            await session.execute(
                select(User).where(User.username == "tester")
            )
        ).scalar_one()
        await _seed_job(
            session, "b-ghours-2", "j-2",
            owner_id=tester.id,
            elapsed_s=7200,
            end_time=datetime.now(timezone.utc) - timedelta(hours=2),
            gpu_count=None,
        )

    r = await client.get("/api/stats/gpu-hours-by-user")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["gpu_hours"] == pytest.approx(2.0, rel=1e-3)


@pytest.mark.asyncio
async def test_gpu_hours_window_excludes_older_jobs(client):
    """days=7 excludes a job that ended 30 days ago."""
    import backend.db as db_mod
    from backend.models import User

    async with db_mod.SessionLocal() as session:
        tester = (
            await session.execute(
                select(User).where(User.username == "tester")
            )
        ).scalar_one()
        # Recent job (1h ago): counts
        await _seed_job(
            session, "b-recent", "j-recent",
            owner_id=tester.id,
            elapsed_s=3600,
            end_time=datetime.now(timezone.utc) - timedelta(hours=1),
            gpu_count=1,
        )
        # Old job (30d ago): should be excluded when days=7
        await _seed_job(
            session, "b-old", "j-old",
            owner_id=tester.id,
            elapsed_s=3600,
            end_time=datetime.now(timezone.utc) - timedelta(days=30),
            gpu_count=1,
        )

    r_wide = await client.get("/api/stats/gpu-hours-by-user?days=60")
    assert r_wide.status_code == 200
    assert r_wide.json()[0]["job_count"] == 2

    r_narrow = await client.get("/api/stats/gpu-hours-by-user?days=7")
    assert r_narrow.status_code == 200
    assert r_narrow.json()[0]["job_count"] == 1


@pytest.mark.asyncio
async def test_gpu_hours_admin_sees_all_non_admin_sees_self(client):
    """Admin sees both users; a non-admin only sees themselves."""
    import backend.db as db_mod
    from backend.models import User

    # tester is the first registered user → auto-admin
    # Register + provision a second user (bob) with his own reporter token
    await client.post(
        "/api/auth/register",
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": "password123",
        },
    )
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "bob", "password": "password123"},
    )
    bob_jwt = login.json()["access_token"]
    tok = await client.post(
        "/api/tokens",
        json={"name": "bob-reporter", "scope": "reporter"},
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    bob_token = tok.json()["token"]

    async with db_mod.SessionLocal() as session:
        tester = (
            await session.execute(
                select(User).where(User.username == "tester")
            )
        ).scalar_one()
        bob = (
            await session.execute(
                select(User).where(User.username == "bob")
            )
        ).scalar_one()

        now = datetime.now(timezone.utc)
        await _seed_job(
            session, "b-admin-1", "j-a1",
            owner_id=tester.id,
            elapsed_s=3600, end_time=now - timedelta(hours=1), gpu_count=1,
        )
        await _seed_job(
            session, "b-admin-2", "j-a2",
            owner_id=bob.id,
            elapsed_s=1800, end_time=now - timedelta(hours=2), gpu_count=1,
        )

    # Admin (tester) sees 2 rows
    r_admin = await client.get("/api/stats/gpu-hours-by-user")
    assert r_admin.status_code == 200
    admin_rows = r_admin.json()
    assert len(admin_rows) == 2
    usernames = {r["username"] for r in admin_rows}
    assert usernames == {"tester", "bob"}

    # Non-admin (bob) sees exactly 1 row — himself
    r_bob = await client.get(
        "/api/stats/gpu-hours-by-user",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert r_bob.status_code == 200
    bob_rows = r_bob.json()
    assert len(bob_rows) == 1
    assert bob_rows[0]["username"] == "bob"
    assert bob_rows[0]["gpu_hours"] == pytest.approx(0.5, rel=1e-3)


@pytest.mark.asyncio
async def test_gpu_hours_non_admin_empty_still_returns_one_row(client):
    """Non-admin with no jobs still sees exactly one row (stable shape)."""
    await client.post(
        "/api/auth/register",
        json={
            "username": "dora",
            "email": "dora@example.com",
            "password": "password123",
        },
    )
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "dora", "password": "password123"},
    )
    dora_jwt = login.json()["access_token"]
    tok = await client.post(
        "/api/tokens",
        json={"name": "dora-reporter", "scope": "reporter"},
        headers={"Authorization": f"Bearer {dora_jwt}"},
    )
    dora_token = tok.json()["token"]

    r = await client.get(
        "/api/stats/gpu-hours-by-user",
        headers={"Authorization": f"Bearer {dora_token}"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["username"] == "dora"
    assert rows[0]["gpu_hours"] == 0.0
    assert rows[0]["job_count"] == 0


@pytest.mark.asyncio
async def test_gpu_hours_days_out_of_range(client):
    """days=0 and days=500 are rejected with 422."""
    r1 = await client.get("/api/stats/gpu-hours-by-user?days=0")
    assert r1.status_code == 422
    r2 = await client.get("/api/stats/gpu-hours-by-user?days=500")
    assert r2.status_code == 422
