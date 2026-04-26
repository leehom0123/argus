"""Per-user notification subscription + token-based unsubscribe endpoints.

Two routers:

* ``me_router`` — ``/api/me/subscriptions`` (auth required). Lets a
  signed-in user list and bulk-upsert per-(project, event_type) opt-ins.
* ``unsubscribe_router`` — ``/api/unsubscribe`` (public). Consumes a
  one-shot token minted by
  :func:`backend.services.notifications_dispatcher.make_unsubscribe_token`
  and flips the matching subscription rows to ``enabled=False``.

Schema and tokens are defined by ``backend/migrations/versions/019_email_system.py``
+ ``backend/backend/services/notifications_dispatcher.make_unsubscribe_token``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.models import (
    EmailUnsubscribeToken,
    NotificationSubscription,
    User,
)
from backend.schemas.email import (
    SubscriptionBulkIn,
    SubscriptionRow,
    UnsubscribeResult,
)
from backend.services.email_templates import SUPPORTED_EVENTS

log = logging.getLogger(__name__)

me_router = APIRouter(prefix="/api/me", tags=["me", "notifications"])
unsubscribe_router = APIRouter(prefix="/api", tags=["notifications"])


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@me_router.get(
    "/subscriptions",
    response_model=list[SubscriptionRow],
    summary="List the caller's per-(project, event_type) email opt-ins",
)
async def list_subscriptions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SubscriptionRow]:
    """Return every subscription row owned by the current user.

    A row with ``project=None`` is the global default for that
    ``event_type``; per-project rows shadow it.
    """
    rows = (
        await db.execute(
            select(NotificationSubscription).where(
                NotificationSubscription.user_id == user.id
            )
        )
    ).scalars().all()
    return [
        SubscriptionRow(
            project=r.project, event_type=r.event_type, enabled=r.enabled
        )
        for r in rows
    ]


@me_router.patch(
    "/subscriptions",
    response_model=list[SubscriptionRow],
    summary="Bulk upsert the caller's subscription rows",
)
async def patch_subscriptions(
    body: SubscriptionBulkIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SubscriptionRow]:
    """Insert / update every (project, event_type) pair in the request.

    Existing rows for the same key are updated in place; missing rows
    are created.  Rows the body does NOT touch are left alone.
    """
    # Validate event_type up front so a single rogue entry rejects the
    # entire patch (avoids partial commits the UI can't reason about).
    bad = [s for s in body.subscriptions if s.event_type not in SUPPORTED_EVENTS]
    if bad:
        names = ", ".join(sorted({b.event_type for b in bad}))
        raise HTTPException(
            status_code=400,
            detail=f"unknown event_type(s): {names}",
        )

    for sub in body.subscriptions:
        existing = (
            await db.execute(
                select(NotificationSubscription)
                .where(NotificationSubscription.user_id == user.id)
                .where(NotificationSubscription.project.is_(sub.project)
                       if sub.project is None
                       else NotificationSubscription.project == sub.project)
                .where(NotificationSubscription.event_type == sub.event_type)
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                NotificationSubscription(
                    user_id=user.id,
                    project=sub.project,
                    event_type=sub.event_type,
                    enabled=sub.enabled,
                    updated_at=_utcnow_iso(),
                )
            )
        else:
            existing.enabled = sub.enabled
            existing.updated_at = _utcnow_iso()
    await db.commit()

    rows = (
        await db.execute(
            select(NotificationSubscription).where(
                NotificationSubscription.user_id == user.id
            )
        )
    ).scalars().all()
    return [
        SubscriptionRow(
            project=r.project, event_type=r.event_type, enabled=r.enabled
        )
        for r in rows
    ]


@unsubscribe_router.get(
    "/unsubscribe",
    summary="Consume an unsubscribe token (one-shot, public)",
)
async def consume_unsubscribe(
    response: Response,
    token: str = Query(..., min_length=8, max_length=128),
    db: AsyncSession = Depends(get_db),
):
    """Look up *token*, flip the matching subscription rows to disabled.

    * Unknown / malformed token → 404 + ``{"detail": "Invalid token"}``.
    * Already-consumed token → 410 ``Gone`` (replay protection).
    * Success → 200 with a small text confirmation; the caller email
      client typically renders this in a tab so a JSON envelope adds
      friction without value.
    """
    row = (
        await db.execute(
            select(EmailUnsubscribeToken).where(
                EmailUnsubscribeToken.token == token
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Invalid token")
    if row.consumed_at is not None:
        # 410 Gone: token existed but has been used.
        return Response(
            status_code=status.HTTP_410_GONE,
            content="Token already consumed.",
            media_type="text/plain",
        )

    target_events = (
        [row.event_type] if row.event_type else list(SUPPORTED_EVENTS)
    )
    for ev in target_events:
        existing = (
            await db.execute(
                select(NotificationSubscription)
                .where(NotificationSubscription.user_id == row.user_id)
                .where(NotificationSubscription.project.is_(None))
                .where(NotificationSubscription.event_type == ev)
            )
        ).scalar_one_or_none()
        now = _utcnow_iso()
        if existing is None:
            db.add(
                NotificationSubscription(
                    user_id=row.user_id,
                    project=None,
                    event_type=ev,
                    enabled=False,
                    updated_at=now,
                )
            )
        else:
            existing.enabled = False
            existing.updated_at = now

    row.consumed_at = _utcnow_iso()
    await db.commit()

    log.info(
        "email.unsubscribe.consumed user_id=%d event_type=%s",
        row.user_id,
        row.event_type or "<all>",
    )
    return Response(
        content=f"Unsubscribed from {row.event_type or 'all email notifications'}.",
        media_type="text/plain",
    )


__all__ = ["me_router", "unsubscribe_router"]
