"""Per-batch email subscription overrides — model, API, and dispatcher.

Scope:

* Model is persistable + JSON-encoded ``event_kinds`` round-trips.
* ``GET`` returns 404 when no override row exists.
* ``PUT`` creates / updates the row idempotently.
* ``DELETE`` removes the override (and is idempotent on a missing row).
* RBAC: a non-owner of the batch gets ``403`` on every verb.
* Dispatcher integration: a present + enabled override with a
  matching ``event_kind`` causes the email to send even when the
  user has no project-level subscription; an enabled override that
  does NOT list the kind suppresses it even when the project default
  would have allowed it.
"""
from __future__ import annotations

import json

import pytest

from backend.db import SessionLocal
from backend.models import (
    Batch,
    BatchEmailSubscription,
    NotificationSubscription,
    User,
)
from backend.services.email_rate_limit import reset_email_bucket_for_tests
from backend.services.email_templates import seed_default_templates
from backend.services.email_worker import reset_metrics_for_tests
from backend.services.notifications_dispatcher import dispatch_email_for_event


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _jwt_headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {client._test_default_jwt}"}


async def _seed_owned_batch(batch_id: str = "batch-sub-1") -> int:
    """Create a batch owned by the default ``tester`` user, return owner id."""
    async with SessionLocal() as db:
        await seed_default_templates(db)
        owner = (await db.execute(User.__table__.select())).first()
        batch = Batch(
            id=batch_id,
            project="proj-sub",
            status="done",
            start_time="2026-04-25T00:00:00Z",
            end_time="2026-04-25T01:00:00Z",
            host="rtx3090-01",
            owner_id=owner.id,
        )
        db.add(batch)
        await db.commit()
        return owner.id


async def _register_second_user(client) -> tuple[str, str]:
    """Register a second user and return ``(jwt, username)``."""
    # Drop the default reporter token so /api/auth/register works without
    # accidentally hitting the rate limiter on the same Authorization.
    saved_auth = client.headers.pop("Authorization", None)
    try:
        reg = await client.post(
            "/api/auth/register",
            json={
                "username": "intruder",
                "email": "intruder@example.com",
                "password": "password123",
            },
        )
        assert reg.status_code == 201, reg.text
        login = await client.post(
            "/api/auth/login",
            json={"username_or_email": "intruder", "password": "password123"},
        )
        assert login.status_code == 200, login.text
        return login.json()["access_token"], "intruder"
    finally:
        if saved_auth is not None:
            client.headers["Authorization"] = saved_auth


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


async def test_model_persists_and_round_trips(client):
    uid = await _seed_owned_batch()
    async with SessionLocal() as db:
        row = BatchEmailSubscription(
            user_id=uid,
            batch_id="batch-sub-1",
            event_kinds=json.dumps(["batch_done", "job_failed"]),
            enabled=True,
            created_at="2026-04-25T00:00:00Z",
            updated_at="2026-04-25T00:00:00Z",
        )
        db.add(row)
        await db.commit()

    async with SessionLocal() as db:
        fetched = await db.get(BatchEmailSubscription, (uid, "batch-sub-1"))
        assert fetched is not None
        assert fetched.enabled is True
        assert json.loads(fetched.event_kinds) == ["batch_done", "job_failed"]


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------


async def test_get_returns_404_when_no_override(client):
    await _seed_owned_batch()
    r = await client.get(
        "/api/batches/batch-sub-1/email-subscription",
        headers=await _jwt_headers(client),
    )
    assert r.status_code == 404


async def test_put_creates_then_updates(client):
    await _seed_owned_batch()
    r = await client.put(
        "/api/batches/batch-sub-1/email-subscription",
        headers=await _jwt_headers(client),
        json={"event_kinds": ["batch_done", "batch_failed"], "enabled": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == "batch-sub-1"
    assert body["event_kinds"] == ["batch_done", "batch_failed"]
    assert body["enabled"] is True

    # GET round-trips the saved row.
    g = await client.get(
        "/api/batches/batch-sub-1/email-subscription",
        headers=await _jwt_headers(client),
    )
    assert g.status_code == 200
    assert g.json()["event_kinds"] == ["batch_done", "batch_failed"]

    # PUT again — same key, no duplicate row, kinds replaced.
    r2 = await client.put(
        "/api/batches/batch-sub-1/email-subscription",
        headers=await _jwt_headers(client),
        json={"event_kinds": ["job_failed"], "enabled": False},
    )
    assert r2.status_code == 200
    assert r2.json()["event_kinds"] == ["job_failed"]
    assert r2.json()["enabled"] is False


async def test_put_rejects_unknown_event_kind(client):
    await _seed_owned_batch()
    r = await client.put(
        "/api/batches/batch-sub-1/email-subscription",
        headers=await _jwt_headers(client),
        json={"event_kinds": ["garbage_kind"], "enabled": True},
    )
    assert r.status_code == 400
    assert "garbage_kind" in r.json()["detail"]


async def test_delete_clears_and_is_idempotent(client):
    await _seed_owned_batch()
    # Seed a row first
    await client.put(
        "/api/batches/batch-sub-1/email-subscription",
        headers=await _jwt_headers(client),
        json={"event_kinds": ["batch_done"], "enabled": True},
    )
    r = await client.delete(
        "/api/batches/batch-sub-1/email-subscription",
        headers=await _jwt_headers(client),
    )
    assert r.status_code == 204
    # GET now 404
    g = await client.get(
        "/api/batches/batch-sub-1/email-subscription",
        headers=await _jwt_headers(client),
    )
    assert g.status_code == 404
    # Second DELETE still 204 (idempotent)
    r2 = await client.delete(
        "/api/batches/batch-sub-1/email-subscription",
        headers=await _jwt_headers(client),
    )
    assert r2.status_code == 204


async def test_non_owner_gets_403(client):
    await _seed_owned_batch()
    intruder_jwt, _ = await _register_second_user(client)
    headers = {"Authorization": f"Bearer {intruder_jwt}"}

    # GET 403
    g = await client.get(
        "/api/batches/batch-sub-1/email-subscription", headers=headers
    )
    assert g.status_code == 403

    # PUT 403
    p = await client.put(
        "/api/batches/batch-sub-1/email-subscription",
        headers=headers,
        json={"event_kinds": ["batch_done"], "enabled": True},
    )
    assert p.status_code == 403

    # DELETE 403
    d = await client.delete(
        "/api/batches/batch-sub-1/email-subscription", headers=headers
    )
    assert d.status_code == 403


async def test_missing_batch_returns_404(client):
    r = await client.get(
        "/api/batches/does-not-exist/email-subscription",
        headers=await _jwt_headers(client),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


async def test_dispatcher_uses_per_batch_override_when_present(client):
    """Override with kind in ``event_kinds`` → email sends even though
    ``batch_done`` defaults to opt-OUT at the project level."""
    reset_email_bucket_for_tests()
    reset_metrics_for_tests()
    uid = await _seed_owned_batch()

    async with SessionLocal() as db:
        db.add(
            BatchEmailSubscription(
                user_id=uid,
                batch_id="batch-sub-1",
                event_kinds=json.dumps(["batch_done"]),
                enabled=True,
                created_at="2026-04-25T00:00:00Z",
                updated_at="2026-04-25T00:00:00Z",
            )
        )
        await db.commit()

    async with SessionLocal() as db:
        batch = await db.get(Batch, "batch-sub-1")
        outcome = await dispatch_email_for_event(
            db, event_type="batch_done", batch=batch,
        )
        await db.commit()
    assert len(outcome.sent_to) == 1


async def test_dispatcher_override_suppresses_default_opt_in(client):
    """Override exists + enabled but ``batch_failed`` not in ``event_kinds``
    → email skipped even though it would default to opt-IN."""
    reset_email_bucket_for_tests()
    reset_metrics_for_tests()
    uid = await _seed_owned_batch()

    async with SessionLocal() as db:
        db.add(
            BatchEmailSubscription(
                user_id=uid,
                batch_id="batch-sub-1",
                event_kinds=json.dumps(["batch_done"]),  # only batch_done
                enabled=True,
                created_at="2026-04-25T00:00:00Z",
                updated_at="2026-04-25T00:00:00Z",
            )
        )
        await db.commit()

    async with SessionLocal() as db:
        batch = await db.get(Batch, "batch-sub-1")
        outcome = await dispatch_email_for_event(
            db, event_type="batch_failed", batch=batch,
        )
        await db.commit()
    assert outcome.sent_to == []
    assert len(outcome.skipped_unsubscribed) == 1


async def test_dispatcher_disabled_override_falls_back_to_project(client):
    """Disabled override is treated as ``no override`` → project default
    applies.  The user has a project-level opt-in for ``batch_done`` so
    the email still sends."""
    reset_email_bucket_for_tests()
    reset_metrics_for_tests()
    uid = await _seed_owned_batch()

    async with SessionLocal() as db:
        # Disabled per-batch override (its event_kinds list does NOT
        # include batch_done — but enabled=False makes the dispatcher
        # ignore the row entirely)
        db.add(
            BatchEmailSubscription(
                user_id=uid,
                batch_id="batch-sub-1",
                event_kinds=json.dumps([]),
                enabled=False,
                created_at="2026-04-25T00:00:00Z",
                updated_at="2026-04-25T00:00:00Z",
            )
        )
        # Project-level opt-in (global default row)
        db.add(
            NotificationSubscription(
                user_id=uid,
                project=None,
                event_type="batch_done",
                enabled=True,
                updated_at="2026-04-25T00:00:00Z",
            )
        )
        await db.commit()

    async with SessionLocal() as db:
        batch = await db.get(Batch, "batch-sub-1")
        outcome = await dispatch_email_for_event(
            db, event_type="batch_done", batch=batch,
        )
        await db.commit()
    assert len(outcome.sent_to) == 1
