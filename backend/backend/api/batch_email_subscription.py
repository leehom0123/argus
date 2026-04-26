"""Per-batch email subscription overrides.

Lets a batch owner override the project-level email defaults for a
single batch.  Three endpoints, all auth required:

* ``GET    /api/batches/{batch_id}/email-subscription`` — return the
  caller's row (``404`` when none → caller falls back to project default).
* ``PUT    /api/batches/{batch_id}/email-subscription`` — upsert the
  row.  Only the BATCH OWNER may write; non-owners get a 403.
* ``DELETE /api/batches/{batch_id}/email-subscription`` — clear the
  override.

The ``BatchEmailSubscription`` table stores ``event_kinds`` as a
JSON-encoded list of strings.  The wire schema keeps it parsed so the
UI never has to know about the encoding.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.models import Batch, BatchEmailSubscription, User
from backend.schemas.email import (
    BatchEmailSubscriptionIn,
    BatchEmailSubscriptionOut,
)
from backend.services.email_templates import SUPPORTED_EVENTS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/batches", tags=["batches", "notifications"])


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_event_kinds(raw: str | None) -> list[str]:
    """Decode the on-disk JSON list, tolerating legacy / corrupt rows."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("batch_email_sub: malformed event_kinds: %r", raw)
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if isinstance(x, str)]


def _row_to_out(row: BatchEmailSubscription) -> BatchEmailSubscriptionOut:
    return BatchEmailSubscriptionOut(
        batch_id=row.batch_id,
        event_kinds=_parse_event_kinds(row.event_kinds),
        enabled=bool(row.enabled),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _load_batch_or_404(db: AsyncSession, batch_id: str) -> Batch:
    """Fetch an undeleted batch, otherwise 404 — same shape as
    ``GET /api/batches/{id}`` so the UI gets a consistent error."""
    batch = await db.get(Batch, batch_id)
    if batch is None or batch.is_deleted:
        raise HTTPException(status_code=404, detail="batch not found")
    return batch


def _require_owner(batch: Batch, user: User) -> None:
    """Reject non-owners with 403.

    Project-level subscriptions handle the "shared user wants alerts"
    case; per-batch overrides are deliberately scoped to the owner so
    the RBAC story stays simple.  Admins are NOT carved out — admins
    can still configure their own project-level subscription if they
    want emails for batches they don't own.
    """
    if batch.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="only the batch owner can manage per-batch email subscriptions",
        )


@router.get(
    "/{batch_id}/email-subscription",
    response_model=BatchEmailSubscriptionOut,
    summary="Return the caller's per-batch email subscription override",
)
async def get_batch_email_subscription(
    batch_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BatchEmailSubscriptionOut:
    """Return ``404`` when no override exists.

    The frontend treats 404 as "fall back to project-level default" and
    renders the project defaults in the checkboxes.
    """
    batch = await _load_batch_or_404(db, batch_id)
    _require_owner(batch, user)

    row = await db.get(BatchEmailSubscription, (user.id, batch_id))
    if row is None:
        raise HTTPException(status_code=404, detail="no override")
    return _row_to_out(row)


@router.put(
    "/{batch_id}/email-subscription",
    response_model=BatchEmailSubscriptionOut,
    summary="Upsert the caller's per-batch email subscription override",
)
async def put_batch_email_subscription(
    batch_id: str,
    body: BatchEmailSubscriptionIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BatchEmailSubscriptionOut:
    """Create or replace the override row for ``(user, batch)``.

    Validates every event_kind against ``SUPPORTED_EVENTS`` so a UI
    typo can't silently disable notifications by writing an unknown
    kind that no event ever matches.  Duplicates within ``event_kinds``
    are deduped (preserving order) so the GET response is stable.
    """
    batch = await _load_batch_or_404(db, batch_id)
    _require_owner(batch, user)

    bad = [k for k in body.event_kinds if k not in SUPPORTED_EVENTS]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unknown event_kind(s): {', '.join(sorted(set(bad)))}",
        )
    seen: set[str] = set()
    deduped: list[str] = []
    for k in body.event_kinds:
        if k not in seen:
            seen.add(k)
            deduped.append(k)

    now = _utcnow_iso()
    encoded = json.dumps(deduped)
    existing = await db.get(BatchEmailSubscription, (user.id, batch_id))
    if existing is None:
        row = BatchEmailSubscription(
            user_id=user.id,
            batch_id=batch_id,
            event_kinds=encoded,
            enabled=body.enabled,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        existing.event_kinds = encoded
        existing.enabled = body.enabled
        existing.updated_at = now
        row = existing
    await db.commit()
    await db.refresh(row)
    return _row_to_out(row)


@router.delete(
    "/{batch_id}/email-subscription",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear the caller's per-batch email subscription override",
)
async def delete_batch_email_subscription(
    batch_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Idempotent — DELETE on a missing override returns 204 anyway.

    Reverts the batch to the user's project-level default.  Returning
    204 even when the row was already absent keeps the UI's "Reset to
    project default" button safe to re-click without surfacing a toast.
    """
    batch = await _load_batch_or_404(db, batch_id)
    _require_owner(batch, user)

    existing = await db.get(BatchEmailSubscription, (user.id, batch_id))
    if existing is not None:
        await db.delete(existing)
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
