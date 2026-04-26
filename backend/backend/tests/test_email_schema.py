"""Team Email — schema smoke tests for migration 019.

Verifies the 5 new tables / ORM classes exist, booleans default the
way the spec requires, and the template seeder populates the
factory-default (event_type, locale) matrix.
"""
from __future__ import annotations

import pytest

from backend.db import SessionLocal
from backend.models import (
    EmailDeadLetter,
    EmailTemplate,
    EmailUnsubscribeToken,
    NotificationSubscription,
    SmtpConfig,
)
from backend.services.email_templates import (
    EVENT_DEFAULTS,
    SUPPORTED_EVENTS,
    SUPPORTED_LOCALES,
    seed_default_templates,
)


pytestmark = pytest.mark.asyncio


async def test_smtp_config_defaults(client):
    """SmtpConfig has enabled=False and use_tls=True by default."""
    async with SessionLocal() as db:
        row = SmtpConfig(id=1)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    assert row.enabled is False
    assert row.use_tls is True
    assert row.use_ssl is False
    assert row.smtp_port == 587


async def test_seed_default_templates_inserts_12(client):
    """The factory seeder inserts 6 events × 2 locales = 12 rows."""
    async with SessionLocal() as db:
        n = await seed_default_templates(db)
        await db.commit()

        rows = (await db.execute(
            EmailTemplate.__table__.select()
        )).all()
    # First call inserts everything, subsequent calls are idempotent.
    assert n == len(EVENT_DEFAULTS) == 12
    assert len(rows) == 12
    # Every (event, locale) present; every row flagged is_system.
    seen = {(r.event_type, r.locale) for r in rows}
    for ev in SUPPORTED_EVENTS:
        for loc in SUPPORTED_LOCALES:
            assert (ev, loc) in seen
    assert all(r.is_system for r in rows)


async def test_seed_default_templates_idempotent(client):
    """Running the seeder twice doesn't duplicate or clobber edits."""
    async with SessionLocal() as db:
        await seed_default_templates(db)
        await db.commit()

    # Edit one row; seeder should not overwrite it.
    async with SessionLocal() as db:
        row = (await db.execute(
            EmailTemplate.__table__.select().where(
                EmailTemplate.event_type == "batch_done",
                EmailTemplate.locale == "en-US",
            )
        )).first()
        assert row is not None
        tpl = await db.get(EmailTemplate, row.id)
        tpl.subject = "custom edited subject"
        await db.commit()

    async with SessionLocal() as db:
        n = await seed_default_templates(db)
        await db.commit()
        tpl = (await db.execute(
            EmailTemplate.__table__.select().where(
                EmailTemplate.event_type == "batch_done",
                EmailTemplate.locale == "en-US",
            )
        )).first()
    assert n == 0  # nothing new
    assert tpl.subject == "custom edited subject"


async def test_notification_subscription_unique(client):
    """Unique index on (user_id, project, event_type) prevents dupes."""
    from sqlalchemy.exc import IntegrityError

    async with SessionLocal() as db:
        u_id = client._test_default_jwt  # type: ignore[attr-defined]
        # Fetch the tester user's id from the DB.
        from backend.models import User
        u = (await db.execute(User.__table__.select())).first()
        uid = u.id

        db.add(NotificationSubscription(
            user_id=uid,
            project="proj-a",
            event_type="batch_done",
            enabled=True,
        ))
        await db.commit()

    async with SessionLocal() as db:
        db.add(NotificationSubscription(
            user_id=uid,
            project="proj-a",
            event_type="batch_done",
            enabled=False,
        ))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_email_dead_letter_columns(client):
    """Dead-letter table accepts the BE-1 column set."""
    async with SessionLocal() as db:
        db.add(EmailDeadLetter(
            to_address="x@y.z",
            subject="fail",
            event_type="batch_failed",
            payload_json="{}",
            attempts=0,
            last_error="connection refused",
            created_at="2026-04-25T00:00:00Z",
        ))
        await db.commit()
        row = (await db.execute(
            EmailDeadLetter.__table__.select()
        )).first()
    assert row is not None
    assert row.attempts == 0
    assert row.event_type == "batch_failed"


async def test_email_unsubscribe_token_roundtrip(client):
    """Token row persists with nullable event_type + consumed_at."""
    async with SessionLocal() as db:
        from backend.models import User
        u = (await db.execute(User.__table__.select())).first()
        db.add(EmailUnsubscribeToken(
            token="deadbeef" * 4,
            user_id=u.id,
            event_type=None,  # unsubscribe-all
            created_at="2026-04-25T00:00:00Z",
        ))
        await db.commit()
        row = await db.get(EmailUnsubscribeToken, "deadbeef" * 4)
    assert row is not None
    assert row.event_type is None
    assert row.consumed_at is None
