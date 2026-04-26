"""``/api/notifications`` — in-app notification bell CRUD.

Four endpoints:
  GET    /api/notifications               list (newest first, optional filter)
  POST   /api/notifications/{id}/ack      mark one read (204)
  POST   /api/notifications/mark_all_read bulk ack (204)
  DELETE /api/notifications/{id}          delete one (204)

All endpoints require authentication. A user can only see and mutate their
own notification rows — accessing another user's row returns 404 (not 403)
to avoid leaking ids.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.models import Notification, User
from backend.utils.response_cache import default_cache as _response_cache

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _bust_notifications_cache(user_id: int) -> None:
    """Drop every cached ``list_notifications`` variant for *user_id*.

    Called after ack / mark_all_read / delete so the unread count + list
    reflect the write on the next read instead of lingering for up to
    10 seconds. The key scheme is ``notifications:u{uid}:<params>`` so a
    prefix bust on ``notifications:u{uid}:`` covers every query-param
    variant (unread_only, limit) without enumeration.
    """
    _response_cache.invalidate_prefix(f"notifications:u{user_id}:")


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


class NotificationOut(BaseModel):
    id: int
    batch_id: str | None
    rule_id: str
    severity: str
    title: str
    body: str
    created_at: str
    read_at: str | None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


async def _get_owned(
    notification_id: int,
    user: User,
    db: AsyncSession,
) -> Notification:
    row = await db.get(Notification, notification_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    unread_only: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[NotificationOut]:
    """Return the authenticated user's notifications, newest first."""
    # Key on user id + query params — ack / mark_all_read bust the prefix
    # so the unread-count pill on the bell icon refreshes immediately.
    key = f"notifications:u{user.id}:limit={limit}:unread={int(unread_only)}"

    async def _load() -> list[NotificationOut]:
        q = (
            select(Notification)
            .where(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
        if unread_only:
            q = q.where(Notification.read_at.is_(None))

        rows = (await db.execute(q)).scalars().all()
        return [NotificationOut.model_validate(r) for r in rows]

    return await _response_cache.get_or_compute(key, _load)


@router.post(
    "/mark_all_read",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def mark_all_read(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Mark every unread notification for the caller as read."""
    now = _utcnow_iso()
    await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id)
        .where(Notification.read_at.is_(None))
        .values(read_at=now)
    )
    await db.commit()
    _bust_notifications_cache(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{notification_id}/ack",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def ack_notification(
    notification_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Mark a single notification as read. No-op if already read."""
    row = await _get_owned(notification_id, user, db)
    if row.read_at is None:
        row.read_at = _utcnow_iso()
        await db.commit()
        _bust_notifications_cache(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_notification(
    notification_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a single notification."""
    # Confirm ownership before deleting (404-not-403 to avoid id enumeration).
    row = await _get_owned(notification_id, user, db)
    await db.delete(row)
    await db.commit()
    _bust_notifications_cache(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
