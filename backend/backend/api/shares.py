"""``/api/batches/{id}/shares`` and ``/api/projects/shares`` endpoints.

Only the batch / project owner (or an admin) can mutate the grant
list. Reads are allowed for the same set plus any user who was already
granted the share — rejecting a grantee's read here would make the UI
unable to render "shared with me" lists.

All writes audit via :class:`AuditService` (``share_add`` / ``share_remove``).
Self-sharing is rejected with 400 since the owner already has access.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, BatchShare, ProjectShare, User
from backend.schemas.shares import (
    BatchShareIn,
    BatchShareOut,
    ProjectShareIn,
    ProjectShareOut,
)
from backend.services.audit import get_audit_service
from backend.utils.response_cache import default_cache as _response_cache

log = logging.getLogger(__name__)


# Batch-read endpoints cache per ``(user, batch)``; when visibility flips
# for a user (share add / remove) we bust every key for that (user, batch)
# pair so the next read reflects the new state instead of serving a stale
# 10-second payload. Listing here is cheap — one dict lookup per prefix.
_BATCH_KEY_PREFIXES: tuple[str, ...] = (
    "batch:",
    "batch-jobs:",
    "batch-health:",
    "batch-eta:",
    "batch-jobs-eta-all:",
    "batch-resources:",
    "batch-epochs-latest:",
    "batch-log-lines:",
)

# Job-scoped read endpoints — keys are ``job<scope>:u{user}:{batch}:{job}``.
# A prefix of ``<scope>:u{user}:{batch}`` is unique enough to drop every
# per-(user, batch) job cache entry without touching unrelated batches.
_JOB_KEY_PREFIXES: tuple[str, ...] = (
    "job:",
    "job-eta:",
    "job-epochs:",
)


def _bust_batch_cache_for_user(batch_id: str, user_id: int) -> None:
    """Drop all per-user batch cache entries for (user_id, batch_id).

    Called after share add/remove (visibility flip) and after batch
    stop / delete / bulk-delete + per-job delete (mutation flip). The
    batch + every job under it gets cleared so the next GET reflects
    the new state instead of waiting out the 10s TTL.
    """
    for p in _BATCH_KEY_PREFIXES:
        # batch-resources keys have a trailing ``:100``; prefix match
        # covers both the plain and the resource-limit form.
        _response_cache.invalidate_prefix(f"{p}u{user_id}:{batch_id}")
    for p in _JOB_KEY_PREFIXES:
        # Job keys are ``<prefix>u{user}:{batch}:{job}`` — drop everything
        # under the (user, batch) tuple in one go.
        _response_cache.invalidate_prefix(f"{p}u{user_id}:{batch_id}")
    # ``batches-list`` + ``batches-compact`` keys are per-user but span
    # the whole batch set; clear the caller's entire cache since any
    # visibility / status flip changes which ids they'd see under
    # scope=shared / scope=all.
    _response_cache.invalidate_prefix(f"batches-list:u{user_id}:")
    _response_cache.invalidate_prefix(f"batches-compact:u{user_id}:")
    # ``jobs-global`` (GET /api/jobs) is per-user and aggregates jobs
    # across every visible batch; a share flip changes the visible set,
    # so clear the caller's entire global-jobs cache too.
    _response_cache.invalidate_prefix(f"jobs-global:u{user_id}:")


def _bust_project_cache_for_user(user_id: int) -> None:
    """Drop every per-user project + dashboard cache entry.

    Called after project share grant / revoke so a freshly-shared
    project surfaces immediately for the grantee instead of waiting out
    the 10s TTL on ``GET /api/projects`` / ``GET /api/projects/{p}`` /
    ``GET /api/dashboard``. The exact key shapes mirror the prefixes
    used by the project + dashboard read handlers (see
    ``backend.api.projects`` and ``backend.api.dashboard``):

    * ``projects-list:u{id}``                — list
    * ``project:u{id}:{project}``            — header detail
    * ``project-active:u{id}:{project}``     — active-batches tab
    * ``project_leaderboard:u{id}:{project}:{metric}``
    * ``project_matrix:u{id}:{project}:{metric}``
    * ``project-resources:u{id}:{project}``
    * ``dashboard:u{id}:{scope}``            — home page aggregate
    """
    # The list endpoint key is exactly ``projects-list:u{id}`` (no
    # trailing colon), so a prefix match on that string clears the only
    # entry for the user.
    _response_cache.invalidate_prefix(f"projects-list:u{user_id}")
    _response_cache.invalidate_prefix(f"project:u{user_id}:")
    _response_cache.invalidate_prefix(f"project-active:u{user_id}:")
    _response_cache.invalidate_prefix(f"project_leaderboard:u{user_id}:")
    _response_cache.invalidate_prefix(f"project_matrix:u{user_id}:")
    _response_cache.invalidate_prefix(f"project-resources:u{user_id}:")
    _response_cache.invalidate_prefix(f"dashboard:u{user_id}:")
    # Project share grants change which batches (and therefore which
    # jobs) the grantee can see under GET /api/jobs (jobs-global).
    _response_cache.invalidate_prefix(f"jobs-global:u{user_id}:")

batch_share_router = APIRouter(
    prefix="/api/batches",
    tags=["shares"],
)

project_share_router = APIRouter(
    prefix="/api/projects/shares",
    tags=["shares"],
)


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


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _load_batch_or_404(
    db: AsyncSession, batch_id: str, locale: SupportedLocale = "en-US"
) -> Batch:
    row = await db.get(Batch, batch_id)
    if row is None or row.is_deleted:
        raise HTTPException(status_code=404, detail=tr(locale, "batch.not_found"))
    return row


def _require_owner_or_admin_for_batch(
    batch: Batch, user: User, locale: SupportedLocale = "en-US"
) -> None:
    if user.is_admin:
        return
    if batch.owner_id == user.id:
        return
    # Hide existence from non-owners (404 instead of 403 when the caller
    # is a grantee — non-owners shouldn't be able to see the share list
    # for someone else's batch).
    raise HTTPException(status_code=403, detail=tr(locale, "share.batch.owner_only"))


async def _lookup_grantee(
    db: AsyncSession, username: str, locale: SupportedLocale = "en-US"
) -> User:
    row = (
        await db.execute(
            select(User).where(func.lower(User.username) == username.lower())
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=tr(locale, "share.user.not_found", username=username),
        )
    if not row.is_active:
        raise HTTPException(
            status_code=400, detail=tr(locale, "share.user.deactivated")
        )
    return row


# ---------------------------------------------------------------------------
# Batch shares
# ---------------------------------------------------------------------------


@batch_share_router.get(
    "/{batch_id}/shares", response_model=list[BatchShareOut]
)
async def list_batch_shares(
    batch_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> list[BatchShareOut]:
    """Return the grant list for a batch.

    Visible to owner + admins only; grantees see their own rows
    indirectly through ``GET /api/batches?scope=shared``.
    """
    batch = await _load_batch_or_404(db, batch_id, locale)
    _require_owner_or_admin_for_batch(batch, user, locale)

    stmt = (
        select(BatchShare, User.username)
        .join(User, User.id == BatchShare.grantee_id)
        .where(BatchShare.batch_id == batch_id)
        .order_by(BatchShare.created_at.asc())
    )
    rows = (await db.execute(stmt)).all()
    out: list[BatchShareOut] = []
    for share, username in rows:
        out.append(
            BatchShareOut(
                batch_id=share.batch_id,
                grantee_id=share.grantee_id,
                grantee_username=username,
                permission=share.permission,
                created_at=share.created_at,
                created_by=share.created_by,
            )
        )
    return out


@batch_share_router.post(
    "/{batch_id}/shares",
    response_model=BatchShareOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_batch_share(
    batch_id: str,
    payload: BatchShareIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> BatchShareOut:
    """Grant a user access to a single batch.

    Re-posting the same (batch, grantee) pair updates the permission
    in place rather than 409-ing — most UIs expect idempotent "grant"
    semantics when you toggle viewer↔editor.
    """
    batch = await _load_batch_or_404(db, batch_id, locale)
    _require_owner_or_admin_for_batch(batch, user, locale)

    grantee = await _lookup_grantee(db, payload.grantee_username, locale)
    if grantee.id == batch.owner_id:
        raise HTTPException(
            status_code=400,
            detail=tr(locale, "share.batch.owner_has_access"),
        )
    if grantee.id == user.id:
        raise HTTPException(
            status_code=400, detail=tr(locale, "share.batch.self")
        )

    existing = await db.get(BatchShare, (batch_id, grantee.id))
    if existing is not None:
        existing.permission = payload.permission
        action = "share_update"
    else:
        existing = BatchShare(
            batch_id=batch_id,
            grantee_id=grantee.id,
            permission=payload.permission,
            created_at=_utcnow_iso(),
            created_by=user.id,
        )
        db.add(existing)
        action = "share_add"
    await db.commit()

    # Visibility flipped — drop any cached per-user payloads so the next
    # read rebuilds them against the current share state.
    _bust_batch_cache_for_user(batch_id, grantee.id)

    await get_audit_service().log(
        action=action,
        user_id=user.id,
        target_type="batch",
        target_id=batch_id,
        metadata={
            "share_type": "batch",
            "grantee_id": grantee.id,
            "grantee_username": grantee.username,
            "permission": payload.permission,
        },
        ip=_client_ip(request),
    )

    if action == "share_add":
        try:
            from backend.services.notifications_dispatcher import (
                dispatch_email_for_event,
            )
            await dispatch_email_for_event(
                db,
                event_type="share_granted",
                batch=batch,
                recipients=[grantee],
                context_extra={
                    "shared_by": {"username": user.username},
                    "project": batch.project,
                    "permission": payload.permission,
                },
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("share_granted email dispatch failed: %r", exc)

    return BatchShareOut(
        batch_id=existing.batch_id,
        grantee_id=existing.grantee_id,
        grantee_username=grantee.username,
        permission=existing.permission,
        created_at=existing.created_at,
        created_by=existing.created_by,
    )


@batch_share_router.delete(
    "/{batch_id}/shares/{grantee_id}", status_code=status.HTTP_200_OK
)
async def remove_batch_share(
    batch_id: str,
    grantee_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> dict[str, object]:
    """Revoke one batch share."""
    batch = await _load_batch_or_404(db, batch_id, locale)
    _require_owner_or_admin_for_batch(batch, user, locale)

    row = await db.get(BatchShare, (batch_id, grantee_id))
    if row is None:
        raise HTTPException(status_code=404, detail=tr(locale, "share.not_found"))

    await db.delete(row)
    await db.commit()

    # Visibility flipped — drop grantee's cached view of this batch.
    _bust_batch_cache_for_user(batch_id, grantee_id)

    await get_audit_service().log(
        action="share_remove",
        user_id=user.id,
        target_type="batch",
        target_id=batch_id,
        metadata={"share_type": "batch", "grantee_id": grantee_id},
        ip=_client_ip(request),
    )

    return {"ok": True, "detail": "revoked"}


# ---------------------------------------------------------------------------
# Project shares
# ---------------------------------------------------------------------------


@project_share_router.get("", response_model=list[ProjectShareOut])
async def list_project_shares(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectShareOut]:
    """Return the caller's outgoing project shares.

    This is the "owner view" — for the inverse ("projects shared with
    me") the frontend should call ``GET /api/batches?scope=shared``
    and group by project.
    """
    stmt = (
        select(ProjectShare, User.username)
        .join(User, User.id == ProjectShare.grantee_id)
        .where(ProjectShare.owner_id == user.id)
        .order_by(ProjectShare.created_at.asc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        ProjectShareOut(
            owner_id=share.owner_id,
            project=share.project,
            grantee_id=share.grantee_id,
            grantee_username=username,
            permission=share.permission,
            created_at=share.created_at,
        )
        for share, username in rows
    ]


@project_share_router.post(
    "",
    response_model=ProjectShareOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_project_share(
    payload: ProjectShareIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> ProjectShareOut:
    """Share every batch in ``(me, project)`` with ``grantee_username``.

    No ownership check on the ``project`` string because project names
    are auto-inferred from ``batch.project`` values — adding a row for
    a project you've never used is harmless (will cover future uploads
    if you ever use that name).
    """
    grantee = await _lookup_grantee(db, payload.grantee_username, locale)
    if grantee.id == user.id:
        raise HTTPException(
            status_code=400, detail=tr(locale, "share.project.self")
        )

    existing = await db.get(
        ProjectShare, (user.id, payload.project, grantee.id)
    )
    if existing is not None:
        existing.permission = payload.permission
        action = "share_update"
    else:
        existing = ProjectShare(
            owner_id=user.id,
            project=payload.project,
            grantee_id=grantee.id,
            permission=payload.permission,
            created_at=_utcnow_iso(),
        )
        db.add(existing)
        action = "share_add"
    await db.commit()

    # Project share grants change what the grantee sees under
    # scope=shared / scope=all — bust their batches-list cache.
    _response_cache.invalidate_prefix(f"batches-list:u{grantee.id}:")
    _response_cache.invalidate_prefix(f"batches-compact:u{grantee.id}:")
    # The new batches feed into GET /api/jobs (jobs-global) too —
    # bust the grantee's global-jobs cache so newly-visible jobs
    # surface immediately instead of waiting out the 10s TTL.
    _response_cache.invalidate_prefix(f"jobs-global:u{grantee.id}:")
    # Also bust project + dashboard caches so the freshly-shared
    # project surfaces on the grantee's next GET /api/projects /
    # GET /api/projects/{p} / GET /api/dashboard.
    _bust_project_cache_for_user(grantee.id)

    await get_audit_service().log(
        action=action,
        user_id=user.id,
        target_type="project",
        target_id=payload.project,
        metadata={
            "share_type": "project",
            "project": payload.project,
            "grantee_id": grantee.id,
            "grantee_username": grantee.username,
            "permission": payload.permission,
        },
        ip=_client_ip(request),
    )

    if action == "share_add":
        try:
            from backend.services.notifications_dispatcher import (
                dispatch_email_for_event,
            )
            await dispatch_email_for_event(
                db,
                event_type="share_granted",
                batch=None,
                recipients=[grantee],
                context_extra={
                    "shared_by": {"username": user.username},
                    "project": payload.project,
                    "permission": payload.permission,
                },
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("share_granted email dispatch failed: %r", exc)

    return ProjectShareOut(
        owner_id=existing.owner_id,
        project=existing.project,
        grantee_id=existing.grantee_id,
        grantee_username=grantee.username,
        permission=existing.permission,
        created_at=existing.created_at,
    )


@project_share_router.delete(
    "/{project}/{grantee_id}", status_code=status.HTTP_200_OK
)
async def remove_project_share(
    project: str,
    grantee_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> dict[str, object]:
    """Revoke a project share owned by the caller."""
    row = await db.get(ProjectShare, (user.id, project, grantee_id))
    if row is None:
        # Admin can also revoke someone else's share — look it up per-owner.
        if user.is_admin:
            # find any row matching (project, grantee_id) and revoke it —
            # admin override path uses an explicit SELECT since the PK
            # requires owner_id.
            stmt = select(ProjectShare).where(
                ProjectShare.project == project,
                ProjectShare.grantee_id == grantee_id,
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=tr(locale, "share.not_found"))

    await db.delete(row)
    await db.commit()

    # Project share revoke flips visibility — bust the grantee's
    # batches-list cache so scope=shared / scope=all reflect reality.
    _response_cache.invalidate_prefix(f"batches-list:u{grantee_id}:")
    _response_cache.invalidate_prefix(f"batches-compact:u{grantee_id}:")
    # Mirror the grant path: drop the project + dashboard caches too so
    # the revoked project disappears from their next read.
    _bust_project_cache_for_user(grantee_id)

    await get_audit_service().log(
        action="share_remove",
        user_id=user.id,
        target_type="project",
        target_id=project,
        metadata={
            "share_type": "project",
            "project": project,
            "grantee_id": grantee_id,
        },
        ip=_client_ip(request),
    )

    return {"ok": True, "detail": "revoked"}
