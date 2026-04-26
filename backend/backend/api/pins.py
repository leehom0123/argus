"""``/api/pins`` — per-user compare-pool.

A "pin" represents a batch the user has parked for side-by-side
inspection in the compare view. The DB has no count constraint; the
service layer caps at :data:`MAX_PINS_PER_USER` because the compare
view's UX supports at most 4 columns.

Visibility: you can only pin a batch you currently can see. Dangling
pins (batch deleted) fall off via the ``user_pin.batch_id`` FK cascade.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, User, UserPin
from backend.schemas.pins import MAX_PINS_PER_USER, PinIn, PinOut
from backend.services.visibility import VisibilityResolver

router = APIRouter(prefix="/api/pins", tags=["pins"])


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@router.get("", response_model=list[PinOut])
async def list_pins(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PinOut]:
    """Return every pinned batch with a minimal summary.

    Pins the user has against batches they can no longer see (share
    revoked, batch soft-deleted) are silently filtered out so the compare
    view never has to deal with 404s mid-render.
    """
    rows = (
        await db.execute(
            select(UserPin, Batch)
            .join(Batch, Batch.id == UserPin.batch_id)
            .where(UserPin.user_id == user.id)
            .where(Batch.is_deleted.is_(False))
            .order_by(UserPin.pinned_at.asc())
        )
    ).all()

    resolver = VisibilityResolver()
    out: list[PinOut] = []
    for pin, batch in rows:
        if not await resolver.can_view_batch(user, batch.id, db):
            # Silently skip — share revoked since pin was made.
            continue
        out.append(
            PinOut.model_validate({
                "batch_id": pin.batch_id,
                "pinned_at": pin.pinned_at,
                "project": batch.project,
                "status": batch.status,
                "n_total": batch.n_total,
                "n_done": batch.n_done or 0,
                "n_failed": batch.n_failed or 0,
                "start_time": batch.start_time,
            })
        )
    return out


@router.post("", response_model=PinOut)
async def add_pin(
    payload: PinIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> PinOut:
    """Pin a batch. Idempotent; enforces a 4-pin cap.

    * Re-posting the same batch_id returns the existing row (200 OK).
    * Hitting the cap returns 400 with a hint so the UI can prompt the
      user to unpin something first.
    * A non-visible batch 404s (same policy as ``GET /api/batches/{id}``).
    """
    resolver = VisibilityResolver()
    if not await resolver.can_view_batch(user, payload.batch_id, db):
        raise HTTPException(status_code=404, detail=tr(locale, "pin.batch.not_found"))

    batch = await db.get(Batch, payload.batch_id)
    # can_view_batch already ruled out missing/deleted, so this is a
    # defensive assertion only.
    if batch is None or batch.is_deleted:
        raise HTTPException(status_code=404, detail=tr(locale, "pin.batch.not_found"))

    existing = await db.get(UserPin, (user.id, payload.batch_id))
    if existing is not None:
        return _to_pin_out(existing, batch)

    # Count current pins. The DB doesn't enforce the cap so the API has to.
    current = (
        await db.execute(
            select(func.count(UserPin.user_id)).where(
                UserPin.user_id == user.id
            )
        )
    ).scalar_one()
    if current >= MAX_PINS_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=tr(locale, "pin.limit_reached", limit=MAX_PINS_PER_USER),
        )

    row = UserPin(
        user_id=user.id,
        batch_id=payload.batch_id,
        pinned_at=_utcnow_iso(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _to_pin_out(row, batch)


@router.delete(
    "/{batch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def remove_pin(
    batch_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Unpin. No-op if the pin doesn't exist."""
    await db.execute(
        delete(UserPin)
        .where(UserPin.user_id == user.id)
        .where(UserPin.batch_id == batch_id)
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _to_pin_out(pin: UserPin, batch: Batch) -> PinOut:
    return PinOut.model_validate({
        "batch_id": pin.batch_id,
        "pinned_at": pin.pinned_at,
        "project": batch.project,
        "status": batch.status,
        "n_total": batch.n_total,
        "n_done": batch.n_done or 0,
        "n_failed": batch.n_failed or 0,
        "start_time": batch.start_time,
    })
