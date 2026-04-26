"""Public share endpoints.

Two surfaces here:

* **Owner-facing** (requires auth): ``POST /api/batches/{id}/public-share``
  to mint a slug, ``DELETE`` to revoke.
* **Anonymous** (no auth): ``GET /api/public/{slug}[...]`` — renders the
  same matrix / epochs data a logged-in user sees, but with all owner
  PII stripped (only ``owner_label = "Shared by user #<id>"`` survives).

Expiry is enforced on every read: past-``expires_at`` rows return 410
Gone. View counts use a single ``UPDATE`` statement so concurrent reads
don't race (no select-then-update).
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.deps import get_current_user, get_db, get_settings_dep
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, Event, Job, ProjectMeta, PublicShare, User
from backend.schemas.project_public import (
    PublicProjectBatch,
    PublicProjectDetail,
    PublicProjectSummary,
)
from backend.schemas.projects import (
    ProjectActiveBatch,
    ProjectLeaderboardRow,
    ProjectMatrixOut,
    ProjectResourcesOut,
)
from backend.schemas.public import (
    PublicBatchOut,
    PublicEpochsOut,
    PublicJobOut,
    PublicShareCreateIn,
    PublicShareOut,
)
from backend.schemas import EpochPoint
from backend.services.audit import get_audit_service
from backend.services.dashboard import DashboardService
from backend.utils.ratelimit import get_public_bucket
from backend.utils.response_cache import default_cache as _response_cache

log = logging.getLogger(__name__)


owner_public_router = APIRouter(
    prefix="/api/batches",
    tags=["public-share"],
)

public_router = APIRouter(prefix="/api/public", tags=["public-read"])

# Public-project routes live on a dedicated router with the deeper
# ``/api/public/projects`` prefix so they're matched before the
# ``/api/public/{slug}`` catch-all below. Included separately in
# ``app.py`` so ordering is explicit.
public_projects_router = APIRouter(
    prefix="/api/public/projects", tags=["public-read"]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return (
        _utcnow()
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.rstrip("Z")
    if cleaned.endswith("+00:00"):
        cleaned = cleaned[:-6]
    try:
        return datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("could not parse ISO timestamp %r", value)
        return None


def _generate_slug() -> str:
    """Return a 20-char URL-safe random slug.

    ``secrets.token_urlsafe(15)`` emits exactly 20 characters of base64url
    (15 bytes → 120 bits of entropy, brute-force-proof). We don't add
    the ``em_`` style prefix because the slug is scoped to public
    sharing alone and embeds naturally in ``/public/<slug>`` URLs.
    """
    return secrets.token_urlsafe(15)


def _build_public_url(base_url: str, slug: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/public/{slug}"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _enforce_public_rate_limit(request: Request, locale: SupportedLocale = Depends(get_locale)) -> None:
    """Apply the per-IP anon bucket to public-read routes.

    Keyed by ``request.client.host`` (or ``"unknown"`` when the peer
    address isn't available, e.g. ASGI test transport). Returns 429 +
    Retry-After seconds on exhaustion; caller has no work to do since
    FastAPI propagates the raised :class:`HTTPException` to the client.
    """
    key = _client_ip(request) or "unknown"
    allowed, retry_after = await get_public_bucket().try_consume(key)
    if not allowed:
        # Ceil to whole seconds per RFC 7231 — clients that don't parse
        # fractional Retry-After then still back off at least long
        # enough to avoid thrash.
        wait = max(1, int(retry_after + 0.999))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=tr(locale, "sse.rate_limit"),
            headers={"Retry-After": str(wait)},
        )


async def _load_batch_for_owner(
    db: AsyncSession, batch_id: str, user: User,
    locale: SupportedLocale = "en-US",
) -> Batch:
    row = await db.get(Batch, batch_id)
    if row is None or row.is_deleted:
        raise HTTPException(status_code=404, detail=tr(locale, "public.batch.not_found"))
    if not (user.is_admin or row.owner_id == user.id):
        raise HTTPException(
            status_code=403,
            detail=tr(locale, "share.public.owner_only"),
        )
    return row


# ---------------------------------------------------------------------------
# Owner-facing: create / revoke public share
# ---------------------------------------------------------------------------


@owner_public_router.post(
    "/{batch_id}/public-share",
    response_model=PublicShareOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_public_share(
    batch_id: str,
    payload: PublicShareCreateIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    locale: SupportedLocale = Depends(get_locale),
) -> PublicShareOut:
    """Mint a new public link.

    Multiple slugs per batch are allowed so the owner can hand
    different collaborators different (possibly differently-scoped)
    URLs. Revoking one doesn't impact the others.
    """
    await _load_batch_for_owner(db, batch_id, user, locale)

    slug = _generate_slug()
    # Vanishingly unlikely collision — retry up to 3 times before
    # surfacing a 500.
    for _ in range(3):
        if (await db.get(PublicShare, slug)) is None:
            break
        slug = _generate_slug()
    else:
        raise HTTPException(
            status_code=500, detail=tr(locale, "share.public.slug_exhausted")
        )

    row = PublicShare(
        slug=slug,
        batch_id=batch_id,
        created_at=_utcnow_iso(),
        created_by=user.id,
        expires_at=payload.expires_at,
        view_count=0,
        last_viewed=None,
    )
    db.add(row)
    await db.commit()

    await get_audit_service().log(
        action="public_share_create",
        user_id=user.id,
        target_type="batch",
        target_id=batch_id,
        metadata={"slug": slug, "expires_at": payload.expires_at},
        ip=_client_ip(request),
    )

    return PublicShareOut(
        slug=slug,
        url=_build_public_url(settings.base_url, slug),
        batch_id=batch_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        view_count=0,
        last_viewed=None,
    )


@owner_public_router.get(
    "/{batch_id}/public-shares", response_model=list[PublicShareOut]
)
async def list_public_shares(
    batch_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    locale: SupportedLocale = Depends(get_locale),
) -> list[PublicShareOut]:
    """List every active public slug for the batch.

    Owner-only; useful for the Settings / Shares page to revoke old
    links whose URLs the owner may have forgotten.
    """
    await _load_batch_for_owner(db, batch_id, user, locale)
    stmt = (
        select(PublicShare)
        .where(PublicShare.batch_id == batch_id)
        .order_by(PublicShare.created_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        PublicShareOut(
            slug=r.slug,
            url=_build_public_url(settings.base_url, r.slug),
            batch_id=r.batch_id,
            created_at=r.created_at,
            expires_at=r.expires_at,
            view_count=r.view_count,
            last_viewed=r.last_viewed,
        )
        for r in rows
    ]


@owner_public_router.delete(
    "/{batch_id}/public-share/{slug}", status_code=status.HTTP_200_OK
)
async def revoke_public_share(
    batch_id: str,
    slug: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> dict[str, object]:
    """Revoke a public share. 404 if slug/batch pair doesn't match."""
    await _load_batch_for_owner(db, batch_id, user, locale)
    row = await db.get(PublicShare, slug)
    if row is None or row.batch_id != batch_id:
        raise HTTPException(status_code=404, detail=tr(locale, "public.share.not_found"))

    await db.delete(row)
    await db.commit()

    await get_audit_service().log(
        action="public_share_revoke",
        user_id=user.id,
        target_type="batch",
        target_id=batch_id,
        metadata={"slug": slug},
        ip=_client_ip(request),
    )
    return {"ok": True, "detail": "revoked"}


# ---------------------------------------------------------------------------
# Anonymous: read batch / jobs / epochs via slug
# ---------------------------------------------------------------------------


async def _resolve_slug(db: AsyncSession, slug: str, locale: SupportedLocale = "en-US") -> PublicShare:
    """Look up a slug, enforce expiry, return the row.

    Also increments view_count + last_viewed atomically via a single
    UPDATE. The caller's ``db`` session is what owns the commit; we
    flush but don't commit here so the resolver can be composed in
    larger reads that want a single transaction.
    """
    row = await db.get(PublicShare, slug)
    if row is None:
        raise HTTPException(status_code=404, detail=tr(locale, "public.share.not_found"))
    if row.expires_at is not None:
        exp = _parse_iso(row.expires_at)
        if exp is not None and exp <= _utcnow():
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=tr(locale, "public.share.expired"),
            )
    # Increment view count atomically (no read-then-write race).
    await db.execute(
        update(PublicShare)
        .where(PublicShare.slug == slug)
        .values(
            view_count=PublicShare.view_count + 1,
            last_viewed=_utcnow_iso(),
        )
    )
    await db.commit()
    return row


def _owner_label(owner_id: int | None) -> str:
    if owner_id is None:
        return "Shared anonymously"
    return f"Shared by user #{owner_id}"


def _job_to_public(job: Job) -> PublicJobOut:
    metrics: dict[str, Any] | None = None
    if job.metrics:
        try:
            metrics = json.loads(job.metrics)
        except json.JSONDecodeError:
            metrics = None
    return PublicJobOut(
        id=job.id,
        batch_id=job.batch_id,
        model=job.model,
        dataset=job.dataset,
        status=job.status,
        start_time=job.start_time,
        end_time=job.end_time,
        elapsed_s=job.elapsed_s,
        metrics=metrics,
    )


@public_router.get(
    "/{slug}",
    response_model=PublicBatchOut,
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def get_public_batch(
    slug: str,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> PublicBatchOut:
    """Anonymous batch detail view — PII stripped."""
    share = await _resolve_slug(db, slug, locale)
    batch = await db.get(Batch, share.batch_id)
    if batch is None or batch.is_deleted:
        raise HTTPException(status_code=404, detail=tr(locale, "public.batch.not_found"))

    # Recompute counters from authoritative job rows so the public view
    # matches what the owner sees.
    result = await db.execute(
        select(Job.status).where(Job.batch_id == batch.id)
    )
    statuses = [r[0] for r in result.all()]
    n_done = sum(1 for s in statuses if s and s.lower() == "done")
    n_failed = sum(1 for s in statuses if s and s.lower() == "failed")

    return PublicBatchOut(
        id=batch.id,
        project=batch.project,
        experiment_type=batch.experiment_type,
        host=batch.host,
        command=batch.command,
        n_total=batch.n_total,
        n_done=n_done,
        n_failed=n_failed,
        status=batch.status,
        start_time=batch.start_time,
        end_time=batch.end_time,
        owner_label=_owner_label(batch.owner_id),
    )


@public_router.get(
    "/{slug}/jobs",
    response_model=list[PublicJobOut],
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def list_public_jobs(
    slug: str,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> list[PublicJobOut]:
    """Anonymous job list — same shape as authenticated list, minus owner."""
    share = await _resolve_slug(db, slug, locale)
    stmt = (
        select(Job)
        .where(Job.batch_id == share.batch_id)
        .order_by(Job.start_time.asc().nullslast(), Job.id.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [_job_to_public(j) for j in rows]


@public_router.get(
    "/{slug}/jobs/{job_id}",
    response_model=PublicJobOut,
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def get_public_job(
    slug: str,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> PublicJobOut:
    """Anonymous single-job detail."""
    share = await _resolve_slug(db, slug, locale)
    job = await db.get(Job, (job_id, share.batch_id))
    if job is None:
        raise HTTPException(status_code=404, detail=tr(locale, "public.job.not_found"))
    return _job_to_public(job)


@public_router.get(
    "/{slug}/jobs/{job_id}/epochs",
    response_model=list[EpochPoint],
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def public_job_epochs(
    slug: str,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> list[EpochPoint]:
    """Anonymous per-epoch timeseries."""
    share = await _resolve_slug(db, slug, locale)
    stmt = (
        select(Event)
        .where(Event.batch_id == share.batch_id)
        .where(Event.job_id == job_id)
        .where(Event.event_type == "job_epoch")
        .order_by(Event.timestamp.asc(), Event.id.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: list[EpochPoint] = []
    for ev in rows:
        data: dict[str, Any] = {}
        if ev.data:
            try:
                parsed = json.loads(ev.data)
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                continue
        if "epoch" not in data:
            continue
        out.append(
            EpochPoint(
                timestamp=ev.timestamp,
                epoch=int(data["epoch"]),
                train_loss=data.get("train_loss"),
                val_loss=data.get("val_loss"),
                lr=data.get("lr"),
                **{
                    k: v
                    for k, v in data.items()
                    if k not in {"epoch", "train_loss", "val_loss", "lr"}
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Anonymous: public demo projects (admin-controlled visibility)
# ---------------------------------------------------------------------------


async def _require_public_project(
    project: str, db: AsyncSession, locale: SupportedLocale
) -> ProjectMeta:
    """Load a :class:`ProjectMeta`, 404 if not published.

    Consistent with batch-detail behaviour: we never differentiate
    "project doesn't exist" from "project isn't public", so anon
    visitors can't probe for private project names.
    """
    svc = DashboardService()
    meta = await svc._project_is_public(project, db)
    if meta is None:
        raise HTTPException(
            status_code=404, detail=tr(locale, "project.not_found")
        )
    return meta


@public_projects_router.get(
    "",
    response_model=list[PublicProjectSummary],
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def list_public_projects(
    db: AsyncSession = Depends(get_db),
) -> list[PublicProjectSummary]:
    """Landing-page list: every currently-public project."""
    # Anonymous + no query params, so a flat key is enough. 10s TTL
    # bounds drift when a project is flipped public/private.
    async def _load() -> list[PublicProjectSummary]:
        svc = DashboardService()
        rows = await svc.public_project_list(db)
        return [PublicProjectSummary.model_validate(r) for r in rows]

    return await _response_cache.get_or_compute(
        "list_public_projects:anon:", _load
    )


@public_projects_router.get(
    "/{project}",
    response_model=PublicProjectDetail,
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def get_public_project(
    project: str,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> PublicProjectDetail:
    """Project header for /demo/<project>."""
    await _require_public_project(project, db, locale)
    svc = DashboardService()
    payload = await svc.public_project_detail(project, db)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=tr(locale, "project.not_found")
        )
    return PublicProjectDetail.model_validate(payload)


@public_projects_router.get(
    "/{project}/leaderboard",
    response_model=list[ProjectLeaderboardRow],
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def get_public_project_leaderboard(
    project: str,
    metric: str = "MSE",
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> list[ProjectLeaderboardRow]:
    await _require_public_project(project, db, locale)
    key = f"public_project_leaderboard:anon:{project}:{metric}"

    async def _load() -> list[ProjectLeaderboardRow]:
        svc = DashboardService()
        rows = await svc.project_leaderboard(
            None, project, db, metric=metric, anonymous=True
        )
        if rows is None:
            raise HTTPException(
                status_code=404, detail=tr(locale, "project.not_found")
            )
        return [ProjectLeaderboardRow.model_validate(r) for r in rows]

    return await _response_cache.get_or_compute(key, _load)


@public_projects_router.get(
    "/{project}/matrix",
    response_model=ProjectMatrixOut,
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def get_public_project_matrix(
    project: str,
    metric: str = "MSE",
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> ProjectMatrixOut:
    await _require_public_project(project, db, locale)
    key = f"public_project_matrix:anon:{project}:{metric}"

    async def _load() -> ProjectMatrixOut:
        svc = DashboardService()
        payload = await svc.project_matrix(
            None, project, db, metric=metric, anonymous=True
        )
        if payload is None:
            raise HTTPException(
                status_code=404, detail=tr(locale, "project.not_found")
            )
        return ProjectMatrixOut.model_validate(payload)

    return await _response_cache.get_or_compute(key, _load)


@public_projects_router.get(
    "/{project}/active-batches",
    response_model=list[ProjectActiveBatch],
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def get_public_project_active_batches(
    project: str,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> list[ProjectActiveBatch]:
    await _require_public_project(project, db, locale)
    svc = DashboardService()
    rows = await svc.project_active_batches(
        None, project, db, anonymous=True
    )
    if rows is None:
        raise HTTPException(
            status_code=404, detail=tr(locale, "project.not_found")
        )
    # Strip ``owner_id`` before returning — ProjectActiveBatch allows
    # None but we don't want to leak the integer id even for public
    # demos. Dashboard service doesn't know the caller is anonymous at
    # the query layer, so we redact at the serialisation boundary.
    for r in rows:
        r["owner_id"] = None
    return [ProjectActiveBatch.model_validate(r) for r in rows]


@public_projects_router.get(
    "/{project}/resources",
    response_model=ProjectResourcesOut,
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def get_public_project_resources(
    project: str,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> ProjectResourcesOut:
    await _require_public_project(project, db, locale)
    svc = DashboardService()
    payload = await svc.project_resources(
        None, project, db, anonymous=True
    )
    if payload is None:
        raise HTTPException(
            status_code=404, detail=tr(locale, "project.not_found")
        )
    return ProjectResourcesOut.model_validate(payload)


@public_projects_router.get(
    "/{project}/batches",
    response_model=list[PublicProjectBatch],
    dependencies=[Depends(_enforce_public_rate_limit)],
)
async def list_public_project_batches(
    project: str,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> list[PublicProjectBatch]:
    """Batch metadata list (no jobs, no events) for /demo/<project>."""
    await _require_public_project(project, db, locale)
    svc = DashboardService()
    rows = await svc.public_project_batches(project, db)
    if rows is None:
        raise HTTPException(
            status_code=404, detail=tr(locale, "project.not_found")
        )
    return [PublicProjectBatch.model_validate(r) for r in rows]
