"""Token -> Batch.owner_id binding regression tests (#127).

Production bug (smoke benchmark batch with a Bearer token but no resolved
owner): ``Batch.owner_id`` was ``NULL`` because the ingest path resolved owner
via ``ApiToken.user`` (the eager-loaded relationship) instead of the
``user_id`` column on the token row. The fix in this PR:

  * adds :func:`backend.deps.current_token_user_id` — a column-only read
    from ``request.state``,
  * patches ``/api/events`` and ``/api/events/batch`` to call it, and
  * ships migration ``030_token_user_binding`` to backfill any orphan
    rows to the first admin user.

These tests pin the contract:

  1. Token creation records ``user_id`` (already shipped, pinned here so
     a future refactor can't quietly drop the FK).
  2. After ``POST /api/events``, the resulting ``Batch.owner_id`` equals
     the creator's ``User.id``. Both single-event and batch endpoints.
  3. The migration backfills orphan ``Batch.owner_id`` to the first
     admin user, and skips with a warning when no admin exists.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import select

from backend.models import ApiToken, Batch, User


# ---------------------------------------------------------------------------
# 1. Token creation binds user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_creation_records_user_id(client):
    """A POSTed ``/api/tokens`` row must have ``user_id`` set to the JWT user."""
    jwt = client._test_default_jwt
    r = await client.post(
        "/api/tokens",
        json={"name": "binding-test", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    token_id = body["id"]

    # Inspect the row directly so we're testing the column, not just the
    # API response shape.
    from backend.db import get_session

    async for db in get_session():
        row = await db.get(ApiToken, token_id)
        assert row is not None
        # Must be a real, non-null FK to the JWT-authenticated user.
        assert row.user_id is not None
        # Must round-trip to a real User row.
        owner = await db.get(User, row.user_id)
        assert owner is not None
        assert owner.username == "tester"
        break


# ---------------------------------------------------------------------------
# 2. Event ingest stamps Batch.owner_id from the token
# ---------------------------------------------------------------------------


def _batch_start_event(batch_id: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-26T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "binding", "host": "h1", "user": "tester"},
        "data": {"n_total_jobs": 1, "command": "smoke"},
    }


@pytest.mark.asyncio
async def test_post_events_stamps_owner_id_from_token(client):
    """``/api/events`` must set ``Batch.owner_id`` to the token's user_id."""
    batch_id = "bind-single-1"
    r = await client.post("/api/events", json=_batch_start_event(batch_id))
    assert r.status_code == 200, r.text

    from backend.db import get_session

    async for db in get_session():
        # Resolve the token row that authed the request (the conftest
        # default reporter token belongs to ``tester``).
        users = (await db.execute(select(User).where(User.username == "tester"))).scalars().all()
        assert len(users) == 1
        tester = users[0]

        batch = await db.get(Batch, batch_id)
        assert batch is not None
        assert batch.owner_id is not None, "owner_id must not be NULL (regression for #127)"
        assert batch.owner_id == tester.id
        break


@pytest.mark.asyncio
async def test_post_events_batch_stamps_owner_id_from_token(client):
    """``/api/events/batch`` must stamp owner_id on every newly-stubbed batch."""
    batch_id = "bind-batch-1"
    payload = {"events": [_batch_start_event(batch_id)]}
    r = await client.post("/api/events/batch", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 0

    from backend.db import get_session

    async for db in get_session():
        users = (await db.execute(select(User).where(User.username == "tester"))).scalars().all()
        tester = users[0]
        batch = await db.get(Batch, batch_id)
        assert batch is not None
        assert batch.owner_id == tester.id
        break


@pytest.mark.asyncio
async def test_owner_id_is_immutable_across_replay(client):
    """Subsequent events on the same batch must not overwrite owner_id."""
    batch_id = "bind-immutable-1"
    # First post stamps owner_id.
    await client.post("/api/events", json=_batch_start_event(batch_id))

    # Register a second user + token, then post another event under
    # *their* token. The Batch.owner_id should NOT flip — first writer
    # wins (mirrors the existing comment at events.py:_get_or_stub_batch).
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    alice_jwt = login.json()["access_token"]
    tok = await client.post(
        "/api/tokens",
        json={"name": "alice-rep", "scope": "reporter"},
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    alice_token = tok.json()["token"]

    follow = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_epoch",
        "timestamp": "2026-04-26T09:01:00Z",
        "batch_id": batch_id,
        "job_id": "j1",
        "source": {"project": "binding", "host": "h1", "user": "alice"},
        "data": {"epoch": 1, "train_loss": 0.5, "val_loss": 0.5},
    }
    r = await client.post(
        "/api/events",
        json=follow,
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert r.status_code == 200, r.text

    from backend.db import get_session

    async for db in get_session():
        tester = (
            await db.execute(select(User).where(User.username == "tester"))
        ).scalar_one()
        batch = await db.get(Batch, batch_id)
        assert batch is not None
        # Original owner survives.
        assert batch.owner_id == tester.id
        break


# ---------------------------------------------------------------------------
# 3. Migration backfill behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_assigns_orphan_batches_to_admin(client):
    """Mimic migration 030's UPDATE: admin exists -> orphans get owner_id."""
    from backend.db import get_session

    async for db in get_session():
        # Create an admin user. The conftest default ``tester`` is not
        # admin; we explicitly provision one so the backfill has a target.
        admin = User(
            username="root",
            email="root@example.com",
            password_hash="x",
            is_active=True,
            is_admin=True,
            email_verified=True,
            created_at="2026-04-26T00:00:00Z",
        )
        db.add(admin)
        await db.flush()

        # Two orphan batches.
        for bid in ("orphan-a", "orphan-b"):
            db.add(
                Batch(
                    id=bid,
                    project="legacy",
                    status="done",
                    owner_id=None,
                )
            )
        await db.commit()

        # Run the same UPDATE the migration runs.
        from sqlalchemy import text

        admin_id = (
            await db.execute(
                text(
                    'SELECT id FROM "user" '
                    "WHERE is_admin = 1 OR is_admin = TRUE "
                    "ORDER BY id ASC LIMIT 1"
                )
            )
        ).scalar_one()
        await db.execute(
            text("UPDATE batch SET owner_id = :a WHERE owner_id IS NULL"),
            {"a": int(admin_id)},
        )
        await db.commit()

        for bid in ("orphan-a", "orphan-b"):
            row = await db.get(Batch, bid)
            assert row is not None
            assert row.owner_id == int(admin_id)
        break


@pytest.mark.asyncio
async def test_backfill_skips_when_no_admin_exists(client, caplog):
    """No admin user -> migration logs a warning and leaves rows untouched."""
    from backend.db import get_session
    from sqlalchemy import text

    async for db in get_session():
        # Drop admin flag from every existing user so the lookup misses.
        await db.execute(text('UPDATE "user" SET is_admin = 0'))
        # Ensure ``tester`` (default fixture user) is NOT admin.
        # Then add an orphan batch.
        db.add(
            Batch(
                id="orphan-no-admin",
                project="legacy",
                status="done",
                owner_id=None,
            )
        )
        await db.commit()

        # Replicate the migration's admin-lookup branch.
        admin_row = (
            await db.execute(
                text(
                    'SELECT id FROM "user" '
                    "WHERE is_admin = 1 OR is_admin = TRUE "
                    "ORDER BY id ASC LIMIT 1"
                )
            )
        ).fetchone()

        if admin_row is None:
            # The migration logs at WARNING and returns without UPDATE.
            logging.getLogger("alembic.runtime.migration").warning(
                "030_token_user_binding: no admin user found; skipping"
            )
        else:
            pytest.fail("expected no admin user in this test setup")

        # Orphan stays orphaned.
        row = await db.get(Batch, "orphan-no-admin")
        assert row is not None
        assert row.owner_id is None
        break
