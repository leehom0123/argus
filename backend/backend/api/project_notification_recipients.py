"""Per-project email recipient list (multi-recipient notifications).

Five endpoints, four authenticated + one public:

* ``GET    /api/projects/{project}/recipients`` — list every recipient
  registered for the project.  Visible to project share viewers
  (so collaborators can audit who is being CC'd) and admins; the
  underlying token field is never exposed.
* ``POST   /api/projects/{project}/recipients`` — add a recipient.
  Owner-or-admin only.  Body validates ``email`` as :class:`EmailStr`
  and rejects duplicates with ``409``.
* ``PATCH  /api/projects/{project}/recipients/{recipient_id}`` —
  partial update (``email`` / ``event_kinds`` / ``enabled``).
  Owner-or-admin only.
* ``DELETE /api/projects/{project}/recipients/{recipient_id}`` —
  remove a recipient.  Owner-or-admin only; idempotent (404 only when
  the row never existed for the project).
* ``GET    /api/unsubscribe/recipient/{token}`` — public, no auth.
  Flips ``enabled=False`` on the matching row so the recipient can
  opt out from the email footer's one-click link.

Owner / viewer semantics
------------------------

Projects are name-keyed strings (no first-class entity), so
"ownership" is derived from :class:`Batch`: a user owns the project
``foo`` iff at least one ``Batch.project='foo' AND owner_id=user.id``
row exists (and is not soft-deleted).  Admins always pass the owner
check.  Project-share grantees pass the read check via
:class:`ProjectShare`.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.models import (
    Batch,
    ProjectNotificationRecipient,
    ProjectShare,
    User,
)
from backend.schemas.email import (
    ProjectRecipientIn,
    ProjectRecipientOut,
    ProjectRecipientPatchIn,
)
from backend.services.email_templates import SUPPORTED_EVENTS

log = logging.getLogger(__name__)


router = APIRouter(tags=["projects", "notifications"])


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _decode_event_kinds(raw: str | None) -> list[str]:
    """Tolerant JSON-list decoder for ``event_kinds`` text columns.

    Mirrors the helper in ``batch_email_subscription`` so a malformed
    legacy row can't make the read endpoint crash.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning(
            "project_recipient: malformed event_kinds: %r", raw
        )
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if isinstance(x, str)]


def _row_to_out(
    row: ProjectNotificationRecipient,
) -> ProjectRecipientOut:
    return ProjectRecipientOut(
        id=row.id,
        project=row.project,
        email=row.email,
        event_kinds=_decode_event_kinds(row.event_kinds),
        enabled=bool(row.enabled),
        added_by_user_id=row.added_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_event_kinds(kinds: list[str]) -> list[str]:
    """Reject unknown event_types and dedupe while preserving order.

    Raises 400 with the offending names so a UI typo surfaces directly
    in the toast instead of silently routing past the dispatcher's
    membership check.
    """
    bad = [k for k in kinds if k not in SUPPORTED_EVENTS]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown event_kind(s): {', '.join(sorted(set(bad)))}"
            ),
        )
    seen: set[str] = set()
    deduped: list[str] = []
    for k in kinds:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return deduped


# ---------------------------------------------------------------------------
# Authorisation helpers
# ---------------------------------------------------------------------------


async def _is_project_owner(
    db: AsyncSession, user: User, project: str
) -> bool:
    """Return True iff ``user`` owns at least one batch under ``project``.

    "Owner" is ``Batch.owner_id == user.id`` for any non-deleted batch
    carrying the project name.  This matches how
    :class:`backend.services.visibility.VisibilityResolver` decides
    write access at the batch level — projects are inferred entities
    so we apply the same rule per-batch and OR across rows.
    """
    row_id = (
        await db.execute(
            select(Batch.id)
            .where(Batch.project == project)
            .where(Batch.owner_id == user.id)
            .where(Batch.is_deleted.is_(False))
            .limit(1)
        )
    ).scalar_one_or_none()
    return row_id is not None


async def _can_read_project(
    db: AsyncSession, user: User, project: str
) -> bool:
    """Return True iff ``user`` may see the recipient list.

    Admins always read; owners always read; users with an active
    :class:`ProjectShare` row (any owner, this project, this grantee)
    read.  No batch-level share carve-out — recipient lists are a
    project-level concern.
    """
    if user.is_admin:
        return True
    if await _is_project_owner(db, user, project):
        return True
    share_row = (
        await db.execute(
            select(ProjectShare.owner_id)
            .where(ProjectShare.project == project)
            .where(ProjectShare.grantee_id == user.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return share_row is not None


async def _require_owner_or_admin(
    db: AsyncSession, user: User, project: str
) -> None:
    """Raise 403 unless ``user`` is admin or a project owner."""
    if user.is_admin:
        return
    if await _is_project_owner(db, user, project):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "only the project owner or an admin can manage recipients"
        ),
    )


async def _require_read_access(
    db: AsyncSession, user: User, project: str
) -> None:
    if not await _can_read_project(db, user, project):
        raise HTTPException(
            status_code=403,
            detail="not allowed to view this project's recipients",
        )


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{project}/recipients",
    response_model=list[ProjectRecipientOut],
    summary="List a project's notification recipients",
)
async def list_recipients(
    project: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectRecipientOut]:
    """Return every recipient row for ``project`` ordered by id.

    Visible to project owners, admins, and project-share grantees so
    collaborators can audit who else is being notified.  Returns an
    empty list when no recipients are configured (NOT a 404 — the
    endpoint shape always exists for any project name).
    """
    await _require_read_access(db, user, project)
    rows = (
        await db.execute(
            select(ProjectNotificationRecipient)
            .where(ProjectNotificationRecipient.project == project)
            .order_by(ProjectNotificationRecipient.id.asc())
        )
    ).scalars().all()
    return [_row_to_out(r) for r in rows]


@router.post(
    "/api/projects/{project}/recipients",
    response_model=ProjectRecipientOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a notification recipient to a project",
)
async def add_recipient(
    project: str,
    body: ProjectRecipientIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectRecipientOut:
    """Create one recipient row for ``(project, email)``.

    Owner-or-admin only.  Duplicate ``(project, email)`` returns 409
    via the ``uq_pnr_project_email`` UNIQUE constraint — the API
    relies on the DB rather than re-querying first because the race
    between SELECT and INSERT under concurrent admins would otherwise
    leak duplicates.
    """
    await _require_owner_or_admin(db, user, project)
    kinds = _validate_event_kinds(list(body.event_kinds))

    # Normalise email to lowercase on write so the unique constraint
    # ``uq_pnr_project_email`` rejects ``Bob@x.com`` after ``bob@x.com``
    # (and vice-versa). The dispatcher already case-folds for dedup
    # (see :mod:`backend.services.notifications_dispatcher`), so without
    # this the storage and dispatch layers disagreed and a project
    # could end up with two rows for the same address.
    email_norm = str(body.email).lower()
    now = _utcnow_iso()
    row = ProjectNotificationRecipient(
        project=project,
        email=email_norm,
        event_kinds=json.dumps(kinds),
        enabled=bool(body.enabled),
        added_by_user_id=user.id,
        unsubscribe_token=secrets.token_urlsafe(24),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"recipient {email_norm} already exists for project "
                f"{project}"
            ),
        )
    await db.refresh(row)
    return _row_to_out(row)


@router.patch(
    "/api/projects/{project}/recipients/{recipient_id}",
    response_model=ProjectRecipientOut,
    summary="Partially update a project recipient",
)
async def update_recipient(
    project: str,
    recipient_id: int,
    body: ProjectRecipientPatchIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectRecipientOut:
    """Update ``email``, ``event_kinds``, or ``enabled`` on one row.

    Owner-or-admin only.  Editing ``email`` re-validates as RFC 5322
    and may collide with an existing ``(project, email)`` pair → 409.
    Other fields apply unconditionally.  Any field the body omits is
    left alone — this matches the PATCH semantics every other Argus
    surface uses.
    """
    await _require_owner_or_admin(db, user, project)
    row = await db.get(ProjectNotificationRecipient, recipient_id)
    if row is None or row.project != project:
        raise HTTPException(status_code=404, detail="recipient not found")

    if body.email is not None:
        # See ``add_recipient`` for the lowercase rationale — keep the
        # write path consistent so PATCH-into-collision behaves the
        # same as a duplicate POST.
        row.email = str(body.email).lower()
    if body.event_kinds is not None:
        row.event_kinds = json.dumps(
            _validate_event_kinds(list(body.event_kinds))
        )
    if body.enabled is not None:
        row.enabled = bool(body.enabled)
    row.updated_at = _utcnow_iso()
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"recipient {row.email} already exists for project "
                f"{project}"
            ),
        )
    await db.refresh(row)
    return _row_to_out(row)


@router.delete(
    "/api/projects/{project}/recipients/{recipient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project recipient",
)
async def delete_recipient(
    project: str,
    recipient_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Hard-delete one recipient row.

    Owner-or-admin only.  Returns 204 on success, 404 when the row
    doesn't exist for ``project`` (an id from a sibling project would
    otherwise let an admin-of-A delete a recipient on project B,
    bypassing the path scoping).
    """
    await _require_owner_or_admin(db, user, project)
    row = await db.get(ProjectNotificationRecipient, recipient_id)
    if row is None or row.project != project:
        raise HTTPException(status_code=404, detail="recipient not found")
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Public unsubscribe endpoint
# ---------------------------------------------------------------------------


unsubscribe_router = APIRouter(tags=["notifications"])


@unsubscribe_router.get(
    "/api/unsubscribe/recipient/{token}",
    summary="One-click unsubscribe for a project recipient",
)
async def consume_recipient_unsubscribe(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Flip ``enabled=False`` on the row matching ``token``.

    Public (no auth).  Idempotent — repeat calls keep returning 200
    so a recipient who clicks the link twice doesn't see an error.
    Unknown / malformed tokens return 404 with a generic plaintext
    body so we never leak the existence of a row.
    """
    if not token or len(token) > 128:
        raise HTTPException(status_code=404, detail="Invalid token")
    row = (
        await db.execute(
            select(ProjectNotificationRecipient).where(
                ProjectNotificationRecipient.unsubscribe_token == token
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Invalid token")

    if row.enabled:
        row.enabled = False
        row.updated_at = _utcnow_iso()
        await db.commit()

    log.info(
        "project_recipient.unsubscribe.consumed project=%s email=%s",
        row.project,
        row.email,
    )
    return Response(
        content=(
            f"Unsubscribed {row.email} from project {row.project} "
            "notifications."
        ),
        media_type="text/plain",
    )


__all__ = ["router", "unsubscribe_router"]
