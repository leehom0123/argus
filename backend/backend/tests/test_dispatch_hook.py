"""Team Email — event → email dispatch hook logic."""
from __future__ import annotations

import pytest

from backend.db import SessionLocal
from backend.models import (
    Batch,
    NotificationSubscription,
    User,
)
from backend.services.email_rate_limit import reset_email_bucket_for_tests
from backend.services.email_templates import seed_default_templates
from backend.services.email_worker import reset_metrics_for_tests
from backend.services.notifications_dispatcher import (
    dispatch_email_for_event,
    make_unsubscribe_token,
)


pytestmark = pytest.mark.asyncio


async def _setup_db_state():
    """Seed templates + create a batch owned by the tester user."""
    async with SessionLocal() as db:
        await seed_default_templates(db)
        tester = (await db.execute(User.__table__.select())).first()
        batch = Batch(
            id="dispatch-batch-1",
            project="proj-a",
            status="done",
            start_time="2026-04-25T00:00:00Z",
            end_time="2026-04-25T01:00:00Z",
            host="rtx3090-01",
            owner_id=tester.id,
        )
        db.add(batch)
        await db.commit()
        return tester.id


async def test_dispatch_enqueues_to_batch_owner(client):
    reset_email_bucket_for_tests()
    reset_metrics_for_tests()
    uid = await _setup_db_state()

    async with SessionLocal() as db:
        batch = await db.get(Batch, "dispatch-batch-1")
        outcome = await dispatch_email_for_event(
            db, event_type="batch_done", batch=batch,
        )
        await db.commit()

    # batch_done defaults to opt-OUT, so owner shouldn't be in sent_to.
    assert outcome.sent_to == []
    assert len(outcome.skipped_unsubscribed) == 1


async def test_dispatch_respects_subscription(client):
    reset_email_bucket_for_tests()
    reset_metrics_for_tests()
    uid = await _setup_db_state()

    # Opt-in via a global-default subscription row.
    async with SessionLocal() as db:
        db.add(NotificationSubscription(
            user_id=uid,
            project=None,
            event_type="batch_done",
            enabled=True,
        ))
        await db.commit()

    async with SessionLocal() as db:
        batch = await db.get(Batch, "dispatch-batch-1")
        outcome = await dispatch_email_for_event(
            db, event_type="batch_done", batch=batch,
        )
        await db.commit()
    assert len(outcome.sent_to) == 1


async def test_dispatch_rate_limited(client):
    reset_email_bucket_for_tests()
    reset_metrics_for_tests()
    uid = await _setup_db_state()

    # Opt-in
    async with SessionLocal() as db:
        db.add(NotificationSubscription(
            user_id=uid,
            project=None,
            event_type="batch_failed",
            enabled=True,
        ))
        await db.commit()

    async with SessionLocal() as db:
        batch = await db.get(Batch, "dispatch-batch-1")
        o1 = await dispatch_email_for_event(
            db, event_type="batch_failed", batch=batch,
        )
        o2 = await dispatch_email_for_event(
            db, event_type="batch_failed", batch=batch,
        )
        await db.commit()

    assert len(o1.sent_to) == 1
    assert o2.skipped_rate_limited == o1.sent_to


async def test_make_unsubscribe_token_persists(client):
    uid = await _setup_db_state()
    async with SessionLocal() as db:
        token = await make_unsubscribe_token(
            db, uid, event_type="batch_done"
        )
        await db.commit()
    assert isinstance(token, str)
    assert len(token) >= 30


async def test_dispatch_critical_opts_in_by_default(client):
    """batch_failed / job_failed / batch_diverged / share_granted
    default to enabled=True so owners get them without any explicit
    subscription row."""
    reset_email_bucket_for_tests()
    reset_metrics_for_tests()
    uid = await _setup_db_state()

    async with SessionLocal() as db:
        batch = await db.get(Batch, "dispatch-batch-1")
        outcome = await dispatch_email_for_event(
            db, event_type="batch_failed", batch=batch,
        )
        await db.commit()
    assert len(outcome.sent_to) == 1
