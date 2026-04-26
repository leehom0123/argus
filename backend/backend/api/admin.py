"""``/api/admin/*`` — admin-only management endpoints.

Every route guards on :func:`backend.deps.require_admin`, so a
non-admin gets 403 even if the dependency chain authenticated them.

Scope here is deliberately narrow: user list + ban/unban, feature
flags, and audit log paging. Per-user detail editing (password reset
on behalf of, role promote/demote) is phase 2.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timedelta, timezone

from backend.deps import get_db, require_admin
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import (
    AuditLog,
    Batch,
    HostMeta,
    ProjectMeta,
    ResourceSnapshot,
    User,
)
from backend.schemas.admin import (
    AdminUserOut,
    FeatureFlagOut,
    FeatureFlagUpdateIn,
)
from backend.schemas.audit import AuditLogOut
from backend.schemas.project_public import (
    PublicProjectMetaOut,
    PublicProjectPublishIn,
)
from backend.services.audit import get_audit_service
from backend.utils.response_cache import default_cache as _response_cache
from backend.services.feature_flags import (
    DEFAULT_FLAGS,
    get_flag,
    list_flags,
    set_flag,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AdminUserOut]:
    """Return all users, newest first."""
    stmt = (
        select(User)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [AdminUserOut.model_validate(u) for u in rows]


@router.post("/users/{user_id}/ban", response_model=AdminUserOut)
async def ban_user(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> AdminUserOut:
    """Deactivate a user (``is_active = False``).

    Banned users can't log in (provider returns None) and their API
    tokens 401 because ``get_current_user`` checks ``user.is_active``.
    Banning yourself is rejected — that'd lock out admin access.
    """
    if user_id == admin.id:
        raise HTTPException(
            status_code=400, detail=tr(locale, "admin.user.self_ban")
        )
    row = await db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=tr(locale, "admin.user.not_found"))

    already_banned = not row.is_active
    row.is_active = False
    await db.commit()

    await get_audit_service().log(
        action="user_ban",
        user_id=admin.id,
        target_type="user",
        target_id=str(user_id),
        metadata={
            "target_username": row.username,
            "already_banned": already_banned,
        },
        ip=_client_ip(request),
    )
    return AdminUserOut.model_validate(row)


@router.post("/users/{user_id}/unban", response_model=AdminUserOut)
async def unban_user(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> AdminUserOut:
    """Re-activate a user (``is_active = True``)."""
    row = await db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=tr(locale, "admin.user.not_found"))

    was_banned = not row.is_active
    row.is_active = True
    # Unban clears lockout too — a user fighting a lockout is
    # effectively banned otherwise.
    row.failed_login_count = 0
    row.locked_until = None
    await db.commit()

    await get_audit_service().log(
        action="user_unban",
        user_id=admin.id,
        target_type="user",
        target_id=str(user_id),
        metadata={
            "target_username": row.username,
            "was_banned": was_banned,
        },
        ip=_client_ip(request),
    )
    return AdminUserOut.model_validate(row)


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------


@router.get("/feature-flags", response_model=list[FeatureFlagOut])
async def list_feature_flags(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[FeatureFlagOut]:
    """Return every flag, merging DB overrides onto the built-in defaults.

    Defaults that have never been written to the DB show up with
    ``updated_at=None`` / ``updated_by=None`` so the UI can render
    them as "default value (never modified)".
    """
    from backend.models import FeatureFlag

    merged = await list_flags(db)
    # Fetch metadata (updated_at / updated_by) for keys that exist.
    meta_rows = (
        await db.execute(select(FeatureFlag))
    ).scalars().all()
    meta = {r.key: r for r in meta_rows}

    out: list[FeatureFlagOut] = []
    for key, value in sorted(merged.items()):
        row = meta.get(key)
        out.append(
            FeatureFlagOut(
                key=key,
                value=value,
                updated_at=row.updated_at if row else None,
                updated_by=row.updated_by if row else None,
            )
        )
    return out


@router.put(
    "/feature-flags/{key}",
    response_model=FeatureFlagOut,
    status_code=status.HTTP_200_OK,
)
async def update_feature_flag(
    key: str,
    payload: FeatureFlagUpdateIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> FeatureFlagOut:
    """Set a flag value.

    Flag keys must be already-known (declared in
    :data:`DEFAULT_FLAGS`) OR look like ``[a-z_]+`` — this stops a
    malicious admin from filling the table with garbage keys but
    leaves room for new flags added in future versions without a
    backend redeploy.
    """
    if key not in DEFAULT_FLAGS and not key.replace("_", "").isalnum():
        raise HTTPException(
            status_code=400,
            detail=tr(locale, "admin.flag.invalid_key", key=key),
        )
    row = await set_flag(db, key, payload.value, updated_by=admin.id)
    await db.commit()

    await get_audit_service().log(
        action="feature_flag_update",
        user_id=admin.id,
        target_type="feature_flag",
        target_id=key,
        metadata={"new_value": payload.value},
        ip=_client_ip(request),
    )

    value = await get_flag(db, key)
    return FeatureFlagOut(
        key=key,
        value=value,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@router.get("/audit-log", response_model=list[AuditLogOut])
async def read_audit_log(
    since: str | None = Query(
        default=None,
        description="ISO 8601 timestamp; only rows at or after are returned.",
    ),
    action: str | None = Query(
        default=None, description="Filter to a specific action string."
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AuditLogOut]:
    """Paginate the audit log, newest first."""
    stmt = select(AuditLog).order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
    if since is not None:
        stmt = stmt.where(AuditLog.timestamp >= since)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    stmt = stmt.offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    out: list[AuditLogOut] = []
    for r in rows:
        out.append(
            AuditLogOut(
                id=r.id,
                user_id=r.user_id,
                action=r.action,
                target_type=r.target_type,
                target_id=r.target_id,
                metadata=r.metadata_json,
                timestamp=r.timestamp,
                ip_address=r.ip_address,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Public-demo projects
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _meta_to_out(meta: ProjectMeta) -> PublicProjectMetaOut:
    return PublicProjectMetaOut(
        project=meta.project,
        is_public=meta.is_public,
        public_description=meta.public_description,
        published_at=meta.published_at,
        published_by_user_id=meta.published_by_user_id,
    )


def _bust_project_cache(project: str) -> None:
    """Drop cached reads that embed ``is_public`` / publish metadata.

    The per-user ``project:u{uid}:{project}`` key scheme means one prefix
    sweep per affected cache family. Also busts the ``projects-list:``
    family since the admin tile shows the publish status on the list
    grid, and the public-projects endpoint that surfaces the same rows.
    """
    _response_cache.invalidate_prefix("project:")
    _response_cache.invalidate_prefix("projects-list:")
    _response_cache.invalidate_prefix("project-active:")
    _response_cache.invalidate_prefix("project-resources:")


@router.post(
    "/projects/{project}/publish",
    response_model=PublicProjectMetaOut,
    status_code=status.HTTP_200_OK,
)
async def publish_project(
    project: str,
    payload: PublicProjectPublishIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> PublicProjectMetaOut:
    """Flag a project as publicly viewable.

    Behaviour is deliberately idempotent: the endpoint upserts. Each
    publish call **resets** ``published_at`` + ``published_by_user_id``
    so admins can see "last published / by whom" in the diagnostic
    list. Re-publishing to update the description therefore also shows
    up as a fresh publish event in the audit log.

    404 if the project string has zero non-deleted batches — we won't
    let admins publish a typo'd project name into the /demo landing.
    """
    # Verify the project actually exists before we let the admin publish.
    exists = (
        await db.execute(
            select(Batch.id)
            .where(Batch.project == project)
            .where(Batch.is_deleted.is_(False))
            .limit(1)
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=404, detail=tr(locale, "project.not_found")
        )

    meta = await db.get(ProjectMeta, project)
    now = _utcnow_iso()
    if meta is None:
        meta = ProjectMeta(
            project=project,
            is_public=True,
            public_description=payload.description,
            published_at=now,
            published_by_user_id=admin.id,
        )
        db.add(meta)
    else:
        meta.is_public = True
        # Only overwrite the description if the caller supplied one.
        # ``None`` is ambiguous (no change vs. clear); treat as "no change"
        # to match the PATCH-style semantics common across our other
        # admin toggles. Callers who want to clear the description can
        # re-publish with an empty string.
        if payload.description is not None:
            meta.public_description = payload.description
        meta.published_at = now
        meta.published_by_user_id = admin.id
    await db.commit()
    await db.refresh(meta)
    _bust_project_cache(project)

    await get_audit_service().log(
        action="project_publish",
        user_id=admin.id,
        target_type="project",
        target_id=project,
        metadata={
            "description_len": (
                len(payload.description) if payload.description else 0
            ),
        },
        ip=_client_ip(request),
    )
    return _meta_to_out(meta)


@router.post(
    "/projects/{project}/unpublish",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unpublish_project(
    project: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> None:
    """Flip ``is_public=False``. 204 even when already unpublished.

    We keep the row (with its historical ``published_at`` /
    ``published_by_user_id``) so re-publishing later doesn't forget
    the audit trail. Hard delete is reserved for project renames.
    """
    meta = await db.get(ProjectMeta, project)
    if meta is not None and meta.is_public:
        meta.is_public = False
        await db.commit()
        _bust_project_cache(project)
        await get_audit_service().log(
            action="project_unpublish",
            user_id=admin.id,
            target_type="project",
            target_id=project,
            metadata={},
            ip=_client_ip(request),
        )
    return None


@router.get(
    "/projects/public", response_model=list[PublicProjectMetaOut]
)
async def list_public_projects_admin(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[PublicProjectMetaOut]:
    """Admin diagnostic: every project that's currently flagged public."""
    rows = (
        await db.execute(
            select(ProjectMeta)
            .where(ProjectMeta.is_public.is_(True))
            .order_by(ProjectMeta.published_at.desc().nullslast())
        )
    ).scalars().all()
    return [_meta_to_out(r) for r in rows]


# ---------------------------------------------------------------------------
# Demo project — force-regenerate the built-in seed fixture.
# ---------------------------------------------------------------------------


@router.post(
    "/demo/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={204: {"description": "Demo fixture regenerated."}},
)
async def reset_demo_project(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Wipe and re-seed the ``__demo_forecast__`` fixture.

    Useful after a schema change or when an admin wants fresh
    timestamps. Returns 204 on success (no body). The underlying
    :func:`backend.demo.seed.seed_demo` is idempotent + transactional,
    so even if ``force=True`` is called concurrently by two admins we
    end up with exactly one fixture copy — the later writer simply
    deletes what the earlier one inserted and writes again.
    """
    from backend.demo import seed_demo

    await seed_demo(db, force=True)
    await get_audit_service().log(
        action="demo_reset",
        user_id=admin.id,
        target_type="project",
        target_id="__demo_forecast__",
        metadata={"scope": "demo_fixture"},
        ip=_client_ip(request),
    )
    return None


# ---------------------------------------------------------------------------
# Retention sweep — manual trigger + status
# ---------------------------------------------------------------------------

import time as _time  # stdlib; avoid shadowing ``time`` in FastAPI context

# Module-level state (ok for single-worker; for multi-worker, move to a DB table)
_retention_state: dict = {
    "last_run_at": None,
    "last_run_stats": None,
}


@router.post(
    "/retention/sweep",
    status_code=status.HTTP_200_OK,
)
async def trigger_retention_sweep(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Run the retention sweeper immediately and return results.

    Useful for testing config changes without waiting for the next
    scheduled run. Returns ``{stats, elapsed_ms}``.
    """
    from backend.config import get_settings
    from backend.retention import sweep_once

    t0 = _time.monotonic()
    stats = await sweep_once(db, get_settings())
    elapsed_ms = int((_time.monotonic() - t0) * 1000)

    _retention_state["last_run_at"] = _utcnow_iso()
    _retention_state["last_run_stats"] = stats

    await get_audit_service().log(
        action="retention_sweep_manual",
        user_id=admin.id,
        target_type="system",
        target_id="retention",
        metadata={"stats": stats, "elapsed_ms": elapsed_ms},
        ip=_client_ip(request),
    )
    return {"stats": stats, "elapsed_ms": elapsed_ms}


@router.get(
    "/retention/status",
    status_code=status.HTTP_200_OK,
)
async def get_retention_status(
    _admin: User = Depends(require_admin),
) -> dict:
    """Return current retention settings + last/next run info.

    ``next_run_at`` is a best-effort estimate based on ``last_run_at``
    and the configured interval. If the loop has not run yet since
    startup it returns ``None``.
    """
    from backend.config import get_settings

    settings = get_settings()
    last_at = _retention_state.get("last_run_at")
    next_at = None
    if last_at is not None:
        from datetime import timedelta

        try:
            last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            next_dt = last_dt + timedelta(
                minutes=settings.retention_sweep_interval_minutes
            )
            next_at = (
                next_dt.replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except Exception:  # noqa: BLE001
            pass

    return {
        "settings": {
            "retention_snapshot_days": settings.retention_snapshot_days,
            "retention_log_line_days": settings.retention_log_line_days,
            "retention_job_epoch_days": settings.retention_job_epoch_days,
            "retention_event_other_days": settings.retention_event_other_days,
            "retention_demo_data_days": settings.retention_demo_data_days,
            "retention_sweep_interval_minutes": settings.retention_sweep_interval_minutes,
        },
        "last_run_at": last_at,
        "last_run_stats": _retention_state.get("last_run_stats"),
        "next_run_at": next_at,
    }


# ---------------------------------------------------------------------------
# Backup cron — status readout (Team A / roadmap #34)
# ---------------------------------------------------------------------------


@router.get(
    "/backup-status",
    status_code=status.HTTP_200_OK,
)
async def get_backup_status(
    _admin: User = Depends(require_admin),
) -> dict:
    """Return the newest backup file + retention stats.

    Shape::

        {
          "enabled": bool,
          "interval_h": int,
          "keep_last_n": int,
          "last_backup_at": "2026-04-24T06:00:00Z" | null,
          "backup_age_h": float | null,
          "recent_files": [
             {"name": "monitor-20260424-0600.db",
              "size_bytes": 123456,
              "mtime": "...Z"},
             ...
          ]
        }

    ``backup_age_h`` is the wall-clock delta between now and the newest
    backup file, so the frontend can render a "Last backup: 2.3 h ago"
    banner and shade it red once it exceeds ``interval_h * 3``.
    """
    from backend.config import get_settings as _gs
    from backend.app import BACKEND_DIR  # local import to avoid cycles

    settings = _gs()
    backup_dir = BACKEND_DIR / "data" / "backups"
    enabled = int(settings.backup_interval_h) > 0

    files: list[dict[str, object]] = []
    last_backup_at: str | None = None
    backup_age_h: float | None = None
    if backup_dir.is_dir():
        entries = sorted(
            backup_dir.glob("monitor-*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in entries[: settings.backup_keep_last_n]:
            st = p.stat()
            mtime_dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            files.append(
                {
                    "name": p.name,
                    "size_bytes": st.st_size,
                    "mtime": mtime_dt.replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
        if entries:
            newest = entries[0]
            newest_dt = datetime.fromtimestamp(
                newest.stat().st_mtime, tz=timezone.utc
            )
            last_backup_at = (
                newest_dt.replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            backup_age_h = round(
                (datetime.now(timezone.utc) - newest_dt).total_seconds() / 3600,
                3,
            )

    return {
        "enabled": enabled,
        "interval_h": int(settings.backup_interval_h),
        "keep_last_n": int(settings.backup_keep_last_n),
        "last_backup_at": last_backup_at,
        "backup_age_h": backup_age_h,
        "recent_files": files,
    }


# ---------------------------------------------------------------------------
# Bulk soft-delete (migration 021) — admin only
# ---------------------------------------------------------------------------


class _BulkSkip(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    reason: str


class _BulkDeleteOut(BaseModel):
    model_config = {"extra": "forbid"}

    deleted: list[str]
    skipped: list[_BulkSkip]


class _BulkProjectsIn(BaseModel):
    model_config = {"extra": "forbid"}

    # 500-id cap (v0.1.3 hardening); pydantic returns 422 above the limit.
    projects: list[str] = Field(max_length=500)


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@router.post(
    "/projects/bulk-delete",
    response_model=_BulkDeleteOut,
    status_code=200,
)
async def bulk_delete_projects(
    payload: _BulkProjectsIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> _BulkDeleteOut:
    """Soft-delete multiple projects in one call. Admin only.

    Cascades like the per-project delete: every non-deleted batch under
    each project is flagged ``is_deleted=True`` so visibility queries
    pick up the change immediately. Per-row decisions are returned in
    ``skipped`` (e.g. ``not_found``, ``already_deleted``).
    """
    if not payload.projects:
        raise HTTPException(status_code=400, detail="projects must be non-empty")

    deleted: list[str] = []
    skipped: list[_BulkSkip] = []
    audit = get_audit_service()
    ip = _client_ip(request)
    now = _utcnow_iso()

    for project in payload.projects:
        meta = await db.get(ProjectMeta, project)
        any_batch = (
            await db.execute(
                select(Batch.id)
                .where(Batch.project == project)
                .where(Batch.is_deleted.is_(False))
                .limit(1)
            )
        ).scalar_one_or_none()
        if meta is None and any_batch is None:
            skipped.append(_BulkSkip(id=project, reason="not_found"))
            continue
        if meta is not None and meta.is_deleted:
            skipped.append(_BulkSkip(id=project, reason="already_deleted"))
            continue
        if meta is None:
            meta = ProjectMeta(project=project, is_deleted=True)
            db.add(meta)
        else:
            meta.is_deleted = True
        await db.execute(
            update(Batch)
            .where(Batch.project == project)
            .where(Batch.is_deleted.is_(False))
            .values(is_deleted=True)
        )
        deleted.append(project)
        audit.log_background(
            action="project_deleted",
            user_id=admin.id,
            target_type="project",
            target_id=project,
            metadata={"project": project, "via": "bulk", "deleted_at": now},
            ip=ip,
        )

    if deleted:
        await db.commit()
        # Visibility now changes for every authed user — drop the
        # whole list / compact / project cache so the next render is
        # consistent.
        _response_cache.invalidate_prefix("projects-list:")
        _response_cache.invalidate_prefix("project:")
        _response_cache.invalidate_prefix("batches-list:")
        _response_cache.invalidate_prefix("batches-compact:")

    return _BulkDeleteOut(deleted=deleted, skipped=skipped)


class _BulkHostsIn(BaseModel):
    model_config = {"extra": "forbid"}

    # 500-id cap (v0.1.3 hardening); pydantic returns 422 above the limit.
    hosts: list[str] = Field(max_length=500)


@router.post(
    "/hosts/bulk-delete",
    response_model=_BulkDeleteOut,
    status_code=200,
)
async def bulk_delete_hosts(
    payload: _BulkHostsIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> _BulkDeleteOut:
    """Soft-delete multiple hosts via the ``host_meta`` table. Admin only.

    Safety (v0.1.3): a host that has reported a resource snapshot in
    the last 10 minutes is treated as still active and routed into
    ``skipped`` with reason ``active``. This prevents accidentally
    hiding a host while jobs are mid-run.
    """
    if not payload.hosts:
        raise HTTPException(status_code=400, detail="hosts must be non-empty")

    deleted: list[str] = []
    skipped: list[_BulkSkip] = []
    audit = get_audit_service()
    ip = _client_ip(request)
    now = _utcnow_iso()

    # Cutoff for the "recently active" guard. ``ResourceSnapshot.timestamp``
    # is stored as an ISO-8601 string, so we compare strings (UTC-normalised
    # values sort lexicographically).
    active_cutoff_iso = (
        (datetime.now(timezone.utc) - timedelta(minutes=10))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    # One IN-query for all candidate hosts: returns the latest snapshot
    # timestamp per host so we don't pay an extra round-trip per row.
    last_seen_rows = (
        await db.execute(
            select(
                ResourceSnapshot.host,
                func.max(ResourceSnapshot.timestamp),
            )
            .where(ResourceSnapshot.host.in_(payload.hosts))
            .group_by(ResourceSnapshot.host)
        )
    ).all()
    last_seen: dict[str, str] = {h: ts for h, ts in last_seen_rows if ts}

    for host in payload.hosts:
        meta = await db.get(HostMeta, host)
        if meta is not None and meta.is_deleted:
            skipped.append(_BulkSkip(id=host, reason="already_deleted"))
            continue
        # Active-host guard — string compare against the 10-min cutoff.
        ts = last_seen.get(host)
        if ts is not None and ts >= active_cutoff_iso:
            skipped.append(_BulkSkip(id=host, reason="active"))
            continue
        if meta is None:
            meta = HostMeta(
                host=host,
                is_deleted=True,
                deleted_at=now,
                deleted_by_user_id=admin.id,
                hidden_at=now,
            )
            db.add(meta)
        else:
            meta.is_deleted = True
            meta.deleted_at = now
            meta.deleted_by_user_id = admin.id
            meta.hidden_at = now
        deleted.append(host)
        audit.log_background(
            action="host_deleted",
            user_id=admin.id,
            target_type="host",
            target_id=host,
            metadata={"host": host, "via": "bulk"},
            ip=ip,
        )

    if deleted:
        await db.commit()

    return _BulkDeleteOut(deleted=deleted, skipped=skipped)
