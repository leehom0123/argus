"""``/api/projects/*`` endpoints.

Five JSON endpoints:

* ``GET /api/projects``                            — list
* ``GET /api/projects/{project}``                  — header detail
* ``GET /api/projects/{project}/active-batches``   — running cards
* ``GET /api/projects/{project}/leaderboard``      — best-metric per (model,dataset)
* ``GET /api/projects/{project}/matrix``           — same data in heatmap shape
* ``GET /api/projects/{project}/resources``        — GPU-hours + heatmap

Two CSV export endpoints:

* ``GET /api/projects/{project}/export.csv``       — leaderboard CSV
* ``GET /api/projects/{project}/export-raw.csv``   — per-job detail CSV

Every read path goes through :class:`DashboardService` which applies
visibility filters. A non-matching user gets 404 (we hide the fact
that the project exists — consistent with batch-detail behaviour).
"""
from __future__ import annotations

import csv
import io
import json
from typing import AsyncIterator

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, Job, ProjectMeta, User
from backend.services.audit import get_audit_service
from backend.schemas.projects import (
    ProjectActiveBatch,
    ProjectDetail,
    ProjectLeaderboardRow,
    ProjectMatrixOut,
    ProjectResourcesOut,
    ProjectSummary,
)
from backend.services.dashboard import DashboardService
from backend.services.visibility import VisibilityResolver
from backend.utils.response_cache import default_cache as _response_cache

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# List + detail
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ProjectSummary])
async def list_projects(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectSummary]:
    """List every project visible to the caller."""
    # Visibility filter is per-user — key on user id to avoid cross-user leaks.
    key = f"projects-list:u{user.id}"

    async def _load() -> list[ProjectSummary]:
        svc = DashboardService()
        rows = await svc.list_projects(user, db)
        return [ProjectSummary.model_validate(r) for r in rows]

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{project}", response_model=ProjectDetail)
async def get_project(
    project: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> ProjectDetail:
    key = f"project:u{user.id}:{project}"

    async def _load() -> ProjectDetail:
        svc = DashboardService()
        detail = await svc.project_detail(user, project, db)
        if detail is None:
            raise HTTPException(
                status_code=404, detail=tr(locale, "project.not_found")
            )
        # Surface ``is_public`` + ``public_description`` to admins only so
        # the Projects UI can render the publish status chip. Regular users
        # see ``None`` for both fields — no information leak.
        if user.is_admin:
            meta = await db.get(ProjectMeta, project)
            detail["is_public"] = bool(meta.is_public) if meta else False
            detail["public_description"] = (
                meta.public_description if meta else None
            )
        else:
            detail["is_public"] = None
            detail["public_description"] = None
        return ProjectDetail.model_validate(detail)

    return await _response_cache.get_or_compute(key, _load)


@router.get(
    "/{project}/active-batches",
    response_model=list[ProjectActiveBatch],
)
async def get_active_batches(
    project: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> list[ProjectActiveBatch]:
    key = f"project-active:u{user.id}:{project}"

    async def _load() -> list[ProjectActiveBatch]:
        svc = DashboardService()
        rows = await svc.project_active_batches(user, project, db)
        if rows is None:
            raise HTTPException(
                status_code=404, detail=tr(locale, "project.not_found")
            )
        return [ProjectActiveBatch.model_validate(r) for r in rows]

    return await _response_cache.get_or_compute(key, _load)


@router.get(
    "/{project}/leaderboard",
    response_model=list[ProjectLeaderboardRow],
)
async def get_project_leaderboard(
    project: str,
    metric: str = Query(default="MSE", min_length=1, max_length=32),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> list[ProjectLeaderboardRow]:
    # Leaderboard rows depend on (user, project, metric) — a shared-to-me
    # user sees a different subset than the owner, so the user id has
    # to be part of the key.
    key = f"project_leaderboard:u{user.id}:{project}:{metric}"

    async def _load() -> list[ProjectLeaderboardRow]:
        svc = DashboardService()
        rows = await svc.project_leaderboard(user, project, db, metric=metric)
        if rows is None:
            raise HTTPException(
                status_code=404, detail=tr(locale, "project.not_found")
            )
        return [ProjectLeaderboardRow.model_validate(r) for r in rows]

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{project}/matrix", response_model=ProjectMatrixOut)
async def get_project_matrix(
    project: str,
    metric: str = Query(default="MSE", min_length=1, max_length=32),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> ProjectMatrixOut:
    key = f"project_matrix:u{user.id}:{project}:{metric}"

    async def _load() -> ProjectMatrixOut:
        svc = DashboardService()
        payload = await svc.project_matrix(user, project, db, metric=metric)
        if payload is None:
            raise HTTPException(
                status_code=404, detail=tr(locale, "project.not_found")
            )
        return ProjectMatrixOut.model_validate(payload)

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{project}/resources", response_model=ProjectResourcesOut)
async def get_project_resources(
    project: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> ProjectResourcesOut:
    key = f"project-resources:u{user.id}:{project}"

    async def _load() -> ProjectResourcesOut:
        svc = DashboardService()
        payload = await svc.project_resources(user, project, db)
        if payload is None:
            raise HTTPException(
                status_code=404, detail=tr(locale, "project.not_found")
            )
        return ProjectResourcesOut.model_validate(payload)

    return await _response_cache.get_or_compute(key, _load)


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------


_STANDARD_METRICS = ("MSE", "MAE", "RMSE", "R2", "PCC", "MAPE")


def _csv_streaming_response(
    rows: list[list[str]], filename: str
) -> StreamingResponse:
    """Encode ``rows`` as CSV and return a streaming attachment response.

    Streaming keeps peak memory flat even on multi-thousand-row exports;
    `csv.writer` writes into an in-memory buffer we flush per row.
    """

    def _generate() -> AsyncIterator[bytes]:
        async def _aiter() -> AsyncIterator[bytes]:
            buf = io.StringIO()
            writer = csv.writer(buf)
            for row in rows:
                writer.writerow(row)
                data = buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
                if data:
                    yield data.encode("utf-8")

        return _aiter()

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


async def _build_project_leaderboard_csv(
    user: User, project: str, db: AsyncSession
) -> list[list[str]]:
    """Produce leaderboard CSV rows: one row per (model, dataset) best.

    Includes every metric on the "standard" list so the row width is
    stable across projects (missing metrics are blank).
    """
    svc = DashboardService()
    resolver = VisibilityResolver()

    # Collect full job rows (not just the best_metric for one metric) so
    # we can emit one line per best per metric. For simplicity + UI
    # consistency, we pick the MSE-best row per (model, dataset) and
    # emit every metric on that row.
    visible_stmt = await resolver.visible_batches_query(user, "all", db=db)
    visible_ids_rows = (
        await db.execute(visible_stmt.with_only_columns(Batch.id))
    ).scalars().all()
    visible_ids = list(visible_ids_rows)
    if not visible_ids:
        return [["batch_id", "model", "dataset", "status", "epochs",
                 "elapsed_s", *_STANDARD_METRICS]]

    rows = (
        await db.execute(
            select(Job)
            .select_from(Job)
            .join(Batch, Batch.id == Job.batch_id)
            .where(Batch.project == project)
            .where(Batch.id.in_(visible_ids))
            .where(Job.is_deleted.is_(False))
        )
    ).scalars().all()

    # Per (model, dataset) keep the lowest-MSE row.
    # Jobs without MSE in their metrics still appear (with empty metric cells).
    best: dict[tuple[str, str], Job] = {}
    for job in rows:
        metrics = _safe_metrics(job.metrics)
        mse = metrics.get("MSE") if metrics else None
        key = (job.model or "", job.dataset or "")
        prior = best.get(key)
        if prior is None:
            best[key] = job
            continue
        prior_metrics = _safe_metrics(prior.metrics) or {}
        prior_mse = prior_metrics.get("MSE")
        # Prefer rows that have MSE; among those, keep the lower value.
        if isinstance(mse, (int, float)) and (
            not isinstance(prior_mse, (int, float)) or mse < prior_mse
        ):
            best[key] = job

    out: list[list[str]] = [
        ["batch_id", "model", "dataset", "status", "epochs", "elapsed_s",
         *_STANDARD_METRICS]
    ]
    for key in sorted(best.keys()):
        job = best[key]
        metrics = _safe_metrics(job.metrics) or {}
        epochs = metrics.get("epochs") or metrics.get("train_epochs")
        out.append([
            job.batch_id,
            job.model or "",
            job.dataset or "",
            job.status or "",
            str(epochs) if epochs is not None else "",
            str(job.elapsed_s) if job.elapsed_s is not None else "",
            *[
                _format_metric(metrics.get(name))
                for name in _STANDARD_METRICS
            ],
        ])
    return out


async def _build_project_raw_csv(
    user: User, project: str, db: AsyncSession
) -> list[list[str]]:
    """One row per (batch, model, dataset, metric) detail line."""
    svc = DashboardService()
    resolver = VisibilityResolver()
    visible_stmt = await resolver.visible_batches_query(user, "all", db=db)
    visible_ids_rows = (
        await db.execute(visible_stmt.with_only_columns(Batch.id))
    ).scalars().all()
    visible_ids = list(visible_ids_rows)
    if not visible_ids:
        return [["batch_id", "job_id", "model", "dataset", "status",
                 "elapsed_s", "metric", "value"]]

    rows = (
        await db.execute(
            select(Job)
            .select_from(Job)
            .join(Batch, Batch.id == Job.batch_id)
            .where(Batch.project == project)
            .where(Batch.id.in_(visible_ids))
            .where(Job.is_deleted.is_(False))
        )
    ).scalars().all()

    out: list[list[str]] = [
        ["batch_id", "job_id", "model", "dataset", "status",
         "elapsed_s", "metric", "value"]
    ]
    for job in rows:
        metrics = _safe_metrics(job.metrics) or {}
        if not metrics:
            out.append([
                job.batch_id,
                job.id,
                job.model or "",
                job.dataset or "",
                job.status or "",
                str(job.elapsed_s) if job.elapsed_s is not None else "",
                "",
                "",
            ])
            continue
        for name, value in metrics.items():
            out.append([
                job.batch_id,
                job.id,
                job.model or "",
                job.dataset or "",
                job.status or "",
                str(job.elapsed_s) if job.elapsed_s is not None else "",
                str(name),
                _format_metric(value),
            ])
    return out


def _safe_metrics(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _format_metric(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


@router.get("/{project}/export.csv")
async def export_project_leaderboard_csv(
    project: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> StreamingResponse:
    """Download the project leaderboard as CSV."""
    svc = DashboardService()
    if not await svc._can_view_project(user, project, db):
        raise HTTPException(status_code=404, detail=tr(locale, "project.not_found"))

    rows = await _build_project_leaderboard_csv(user, project, db)
    safe = project.replace("/", "_").replace(" ", "_")
    return _csv_streaming_response(rows, f"{safe}_leaderboard.csv")


# ---------------------------------------------------------------------------
# Soft delete (migration 021) — admin only
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@router.delete("/{project}", status_code=200)
async def delete_project(
    project: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> dict:
    """Soft-delete a project.

    Project deletion is heavy — it implicitly hides every batch under
    the name — so it's restricted to admins. The implementation:

    1. Upsert ``ProjectMeta(project=..., is_deleted=True)``.
    2. Cascade by flipping ``Batch.is_deleted=True`` on every batch
       with that project name (the visibility resolver already filters
       on ``is_deleted`` so the cascade is purely additive).

    Returns 404 when the project has no visible batches and no meta
    row, matching the rest of the project surface.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail=tr(locale, "admin.privileges_required"),
        )

    # Confirm the project exists from the admin's view (i.e. at least
    # one non-deleted batch carries the name OR a meta row exists).
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
        raise HTTPException(
            status_code=404, detail=tr(locale, "project.not_found")
        )

    now = _utcnow_iso()
    if meta is None:
        meta = ProjectMeta(project=project, is_deleted=True)
        db.add(meta)
    else:
        meta.is_deleted = True

    # Cascade to the batches.
    await db.execute(
        update(Batch)
        .where(Batch.project == project)
        .where(Batch.is_deleted.is_(False))
        .values(is_deleted=True)
    )

    await db.commit()

    get_audit_service().log_background(
        action="project_deleted",
        user_id=user.id,
        target_type="project",
        target_id=project,
        metadata={"project": project, "deleted_at": now},
        ip=(request.client.host if request.client else None),
    )

    return {"status": "deleted", "project": project}


@router.get("/{project}/export-raw.csv")
async def export_project_raw_csv(
    project: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> StreamingResponse:
    """Download per-job raw metric rows as CSV."""
    svc = DashboardService()
    if not await svc._can_view_project(user, project, db):
        raise HTTPException(status_code=404, detail=tr(locale, "project.not_found"))

    rows = await _build_project_raw_csv(user, project, db)
    safe = project.replace("/", "_").replace(" ", "_")
    return _csv_streaming_response(rows, f"{safe}_raw.csv")
