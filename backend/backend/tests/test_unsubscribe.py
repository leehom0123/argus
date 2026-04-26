"""Team Email — /api/unsubscribe token consumption."""
from __future__ import annotations

import pytest

from backend.db import SessionLocal
from backend.models import (
    EmailUnsubscribeToken,
    NotificationSubscription,
    User,
)
from backend.services.notifications_dispatcher import make_unsubscribe_token


pytestmark = pytest.mark.asyncio


async def _mint_token(event_type=None):
    async with SessionLocal() as db:
        user = (await db.execute(User.__table__.select())).first()
        token = await make_unsubscribe_token(
            db, user.id, event_type=event_type
        )
        await db.commit()
    return token, user.id


async def test_unsubscribe_invalid_token(client):
    # Anonymous call, no token → 404
    client.headers.pop("Authorization", None)
    r = await client.get(
        "/api/unsubscribe", params={"token": "deadbeef" * 4}
    )
    assert r.status_code == 404
    assert "Invalid" in r.text


async def test_unsubscribe_consumes_and_disables(client):
    token, uid = await _mint_token(event_type="batch_done")
    client.headers.pop("Authorization", None)
    r = await client.get("/api/unsubscribe", params={"token": token})
    assert r.status_code == 200
    assert "Unsubscribed" in r.text

    # The subscription row now exists with enabled=False.
    async with SessionLocal() as db:
        rows = (await db.execute(
            NotificationSubscription.__table__.select()
        )).all()
    matching = [
        r for r in rows
        if r.user_id == uid
        and r.project is None
        and r.event_type == "batch_done"
    ]
    assert len(matching) == 1
    assert matching[0].enabled is False

    # Replay → 410 Gone
    r2 = await client.get("/api/unsubscribe", params={"token": token})
    assert r2.status_code == 410


async def test_unsubscribe_all_events(client):
    # event_type=None token flips EVERY supported event to disabled.
    token, uid = await _mint_token(event_type=None)
    client.headers.pop("Authorization", None)
    r = await client.get("/api/unsubscribe", params={"token": token})
    assert r.status_code == 200

    from backend.services.email_templates import SUPPORTED_EVENTS
    async with SessionLocal() as db:
        rows = (await db.execute(
            NotificationSubscription.__table__.select()
        )).all()
    for ev in SUPPORTED_EVENTS:
        match = [
            r for r in rows
            if r.user_id == uid
            and r.project is None
            and r.event_type == ev
        ]
        assert len(match) == 1, f"missing event {ev}"
        assert match[0].enabled is False
