"""Per-project multi-recipient email subscription (v0.1.4).

Coverage:

* ``POST`` creates a row, owner-or-admin only.
* ``GET`` returns the list (visible to viewers + owners + admins).
* ``PATCH`` updates ``event_kinds`` + ``enabled`` (and ``email``).
* ``DELETE`` removes a row, idempotent on a missing one (404).
* ``email`` validation rejects malformed addresses with 422.
* UNIQUE ``(project, email)``: second add of the same address → 409.
* Public unsubscribe-by-token endpoint flips ``enabled=False`` and
  is idempotent on repeat clicks.
* Dispatcher: a per-project recipient receives the email even when
  no Argus user has it.
* Dispatcher: dedup vs ``ProjectSubscription`` user — same address
  is not double-sent.
"""
from __future__ import annotations

import json

import pytest

from backend.db import SessionLocal
from backend.models import (
    Batch,
    NotificationSubscription,
    ProjectNotificationRecipient,
    User,
)
from backend.services.email_rate_limit import reset_email_bucket_for_tests
from backend.services.email_templates import seed_default_templates
from backend.services.email_worker import reset_metrics_for_tests
from backend.services.notifications_dispatcher import (
    dispatch_email_for_event,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _jwt_headers(client) -> dict[str, str]:
    """Return JWT headers for the default ``tester`` (also admin)."""
    return {"Authorization": f"Bearer {client._test_default_jwt}"}


async def _seed_owned_batch(
    project: str = "proj-recipients", batch_id: str = "batch-recip-1"
) -> int:
    """Seed a batch owned by ``tester``; return the owner id."""
    async with SessionLocal() as db:
        await seed_default_templates(db)
        owner = (await db.execute(User.__table__.select())).first()
        batch = Batch(
            id=batch_id,
            project=project,
            status="done",
            start_time="2026-04-25T00:00:00Z",
            end_time="2026-04-25T01:00:00Z",
            host="rtx3090-01",
            owner_id=owner.id,
        )
        db.add(batch)
        await db.commit()
        return owner.id


async def _register_second_user(
    client, *, username: str = "intruder", email: str = "intruder@example.com"
) -> str:
    """Register a fresh user (NOT admin since ``tester`` was first), return JWT."""
    saved_auth = client.headers.pop("Authorization", None)
    try:
        reg = await client.post(
            "/api/auth/register",
            json={
                "username": username,
                "email": email,
                "password": "password123",
            },
        )
        assert reg.status_code == 201, reg.text
        login = await client.post(
            "/api/auth/login",
            json={"username_or_email": username, "password": "password123"},
        )
        assert login.status_code == 200, login.text
        return login.json()["access_token"]
    finally:
        if saved_auth is not None:
            client.headers["Authorization"] = saved_auth


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


async def test_post_creates_row_and_get_returns_it(client):
    await _seed_owned_batch()
    r = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "alice@example.com",
            "event_kinds": ["batch_done", "batch_failed"],
            "enabled": True,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "alice@example.com"
    assert body["project"] == "proj-recipients"
    assert body["event_kinds"] == ["batch_done", "batch_failed"]
    assert body["enabled"] is True
    assert body["id"] >= 1
    # Token must NOT leak in the response — only in the email footer.
    assert "unsubscribe_token" not in body

    g = await client.get(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
    )
    assert g.status_code == 200
    rows = g.json()
    assert len(rows) == 1
    assert rows[0]["email"] == "alice@example.com"


async def test_post_rejects_non_owner(client):
    await _seed_owned_batch()
    intruder = await _register_second_user(client)
    r = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers={"Authorization": f"Bearer {intruder}"},
        json={
            "email": "bob@example.com",
            "event_kinds": ["batch_done"],
            "enabled": True,
        },
    )
    assert r.status_code == 403, r.text


async def test_get_visible_to_project_share_viewer(client):
    """Project-share grantees can audit who is being notified."""
    from backend.models import ProjectShare

    owner_id = await _seed_owned_batch()
    grantee_jwt = await _register_second_user(
        client, username="viewer", email="viewer@example.com"
    )
    # Look up the grantee by username to grant them a project share.
    async with SessionLocal() as db:
        from sqlalchemy import select

        grantee = (
            await db.execute(select(User).where(User.username == "viewer"))
        ).scalar_one()
        db.add(
            ProjectShare(
                owner_id=owner_id,
                project="proj-recipients",
                grantee_id=grantee.id,
                permission="viewer",
                created_at="2026-04-25T00:00:00Z",
            )
        )
        await db.commit()

    # Owner adds a recipient
    await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "alice@example.com",
            "event_kinds": ["batch_done"],
            "enabled": True,
        },
    )

    # Grantee can read
    r = await client.get(
        "/api/projects/proj-recipients/recipients",
        headers={"Authorization": f"Bearer {grantee_jwt}"},
    )
    assert r.status_code == 200
    assert len(r.json()) == 1

    # …but cannot modify
    p = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers={"Authorization": f"Bearer {grantee_jwt}"},
        json={
            "email": "carol@example.com",
            "event_kinds": ["batch_done"],
            "enabled": True,
        },
    )
    assert p.status_code == 403


async def test_patch_updates_event_kinds_and_enabled(client):
    await _seed_owned_batch()
    create = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "alice@example.com",
            "event_kinds": ["batch_done"],
            "enabled": True,
        },
    )
    rid = create.json()["id"]

    p = await client.patch(
        f"/api/projects/proj-recipients/recipients/{rid}",
        headers=await _jwt_headers(client),
        json={
            "event_kinds": ["batch_failed", "job_failed"],
            "enabled": False,
        },
    )
    assert p.status_code == 200, p.text
    body = p.json()
    assert body["event_kinds"] == ["batch_failed", "job_failed"]
    assert body["enabled"] is False
    # email unchanged
    assert body["email"] == "alice@example.com"


async def test_delete_removes_then_404_on_repeat(client):
    await _seed_owned_batch()
    create = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "alice@example.com",
            "event_kinds": ["batch_done"],
            "enabled": True,
        },
    )
    rid = create.json()["id"]

    d = await client.delete(
        f"/api/projects/proj-recipients/recipients/{rid}",
        headers=await _jwt_headers(client),
    )
    assert d.status_code == 204

    g = await client.get(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
    )
    assert g.status_code == 200
    assert g.json() == []

    # Second DELETE → 404 (the row no longer exists; this differs from
    # batch-email-subscription where there's an implicit (user, batch)
    # PK, but here the surrogate id makes "missing" the only signal).
    d2 = await client.delete(
        f"/api/projects/proj-recipients/recipients/{rid}",
        headers=await _jwt_headers(client),
    )
    assert d2.status_code == 404


async def test_email_validation_rejects_garbage(client):
    await _seed_owned_batch()
    r = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "not-an-email",
            "event_kinds": ["batch_done"],
            "enabled": True,
        },
    )
    assert r.status_code == 422


async def test_unique_project_email_returns_409(client):
    await _seed_owned_batch()
    r1 = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "alice@example.com",
            "event_kinds": ["batch_done"],
            "enabled": True,
        },
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "alice@example.com",
            "event_kinds": ["batch_failed"],
            "enabled": True,
        },
    )
    assert r2.status_code == 409


async def test_email_case_insensitive_collision_returns_409(client):
    """``Bob@X.com`` then ``bob@x.com`` → 409 (storage normalises lc).

    The dispatcher fan-out case-folds for dedup; the API write path
    must do the same so storage and dispatch never disagree on whether
    a given address is "already subscribed".  Backfill of pre-existing
    mixed-case rows is out of scope for this nit.
    """
    await _seed_owned_batch()
    r1 = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "Bob@X.com",
            "event_kinds": ["batch_done"],
            "enabled": True,
        },
    )
    assert r1.status_code == 201, r1.text
    # Stored as the lowercase form, regardless of input casing.
    assert r1.json()["email"] == "bob@x.com"

    r2 = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "bob@x.com",
            "event_kinds": ["batch_failed"],
            "enabled": True,
        },
    )
    assert r2.status_code == 409, r2.text


# ---------------------------------------------------------------------------
# Public unsubscribe endpoint
# ---------------------------------------------------------------------------


async def test_unsubscribe_token_disables_recipient(client):
    await _seed_owned_batch()
    create = await client.post(
        "/api/projects/proj-recipients/recipients",
        headers=await _jwt_headers(client),
        json={
            "email": "alice@example.com",
            "event_kinds": ["batch_done"],
            "enabled": True,
        },
    )
    rid = create.json()["id"]
    # Pull the token directly from the DB (the API never returns it).
    async with SessionLocal() as db:
        row = await db.get(ProjectNotificationRecipient, rid)
        assert row is not None
        token = row.unsubscribe_token

    # Public endpoint, no auth.
    saved_auth = client.headers.pop("Authorization", None)
    try:
        r = await client.get(f"/api/unsubscribe/recipient/{token}")
    finally:
        if saved_auth is not None:
            client.headers["Authorization"] = saved_auth
    assert r.status_code == 200
    assert "alice@example.com" in r.text

    # The row is now disabled.
    async with SessionLocal() as db:
        row = await db.get(ProjectNotificationRecipient, rid)
        assert row is not None
        assert row.enabled is False

    # Idempotent: a second click stays 200.
    saved_auth = client.headers.pop("Authorization", None)
    try:
        r2 = await client.get(f"/api/unsubscribe/recipient/{token}")
    finally:
        if saved_auth is not None:
            client.headers["Authorization"] = saved_auth
    assert r2.status_code == 200


async def test_unsubscribe_unknown_token_404(client):
    saved_auth = client.headers.pop("Authorization", None)
    try:
        r = await client.get("/api/unsubscribe/recipient/not-a-real-token")
    finally:
        if saved_auth is not None:
            client.headers["Authorization"] = saved_auth
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


async def test_dispatcher_sends_to_external_recipient(client):
    """A recipient with no Argus user account still receives the email."""
    reset_email_bucket_for_tests()
    reset_metrics_for_tests()
    owner_id = await _seed_owned_batch()

    async with SessionLocal() as db:
        db.add(
            ProjectNotificationRecipient(
                project="proj-recipients",
                email="external-vendor@partner.example",
                event_kinds=json.dumps(["batch_done"]),
                enabled=True,
                added_by_user_id=owner_id,
                unsubscribe_token="tkn-vendor-1",
                created_at="2026-04-25T00:00:00Z",
                updated_at="2026-04-25T00:00:00Z",
            )
        )
        # The owner himself opts OUT of the email so we can isolate
        # the recipient-list send: subscription says enabled=False.
        db.add(
            NotificationSubscription(
                user_id=owner_id,
                project=None,
                event_type="batch_done",
                enabled=False,
                updated_at="2026-04-25T00:00:00Z",
            )
        )
        await db.commit()

    async with SessionLocal() as db:
        batch = await db.get(Batch, "batch-recip-1")
        outcome = await dispatch_email_for_event(
            db, event_type="batch_done", batch=batch,
        )
        await db.commit()
    assert "external-vendor@partner.example" in outcome.sent_to


async def test_dispatcher_dedup_against_subscription_user(client):
    """Same email via subscription + recipient list → one send only."""
    reset_email_bucket_for_tests()
    reset_metrics_for_tests()
    owner_id = await _seed_owned_batch()

    async with SessionLocal() as db:
        # Owner opts IN globally for batch_done so the subscription
        # path will fire for tester@example.com.
        db.add(
            NotificationSubscription(
                user_id=owner_id,
                project=None,
                event_type="batch_done",
                enabled=True,
                updated_at="2026-04-25T00:00:00Z",
            )
        )
        # Recipient row also lists the owner's email (the case the
        # dedup is meant to catch).
        db.add(
            ProjectNotificationRecipient(
                project="proj-recipients",
                email="tester@example.com",  # owner's own email
                event_kinds=json.dumps(["batch_done"]),
                enabled=True,
                added_by_user_id=owner_id,
                unsubscribe_token="tkn-dedup-1",
                created_at="2026-04-25T00:00:00Z",
                updated_at="2026-04-25T00:00:00Z",
            )
        )
        await db.commit()

    async with SessionLocal() as db:
        batch = await db.get(Batch, "batch-recip-1")
        outcome = await dispatch_email_for_event(
            db, event_type="batch_done", batch=batch,
        )
        await db.commit()

    # Exactly one send to tester@example.com (dedup absorbed the second).
    sends = [
        e for e in outcome.sent_to if e == "tester@example.com"
    ]
    assert len(sends) == 1
    assert "tester@example.com" in outcome.skipped_duplicate_email
