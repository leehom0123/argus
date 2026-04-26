"""Single-job detail + epoch timeseries + per-job ETA."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_session
from backend.deps import get_current_user
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, Event, Job, User
from backend.schemas import EpochPoint, JobOut
from backend.services.audit import get_audit_service
from backend.services.eta import compute_job_eta
from backend.services.visibility import VisibilityResolver
from backend.utils.response_cache import default_cache as _response_cache

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _coerce_float(v: Any) -> float | None:
    """Coerce a metrics value to float, or None on any failure."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _coerce_int(v: Any) -> int | None:
    """Coerce a metrics value to int, or None on any failure."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except ValueError:
            return None
    return None


def _extract_job_extras(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """Pull #21 hover-card fields out of the raw metrics dict.

    Returns a dict with three keys; each value is the parsed number or
    ``None`` when the reporter did not surface it. Key aliases match the
    reporter's evolving conventions — ``Avg_Batch_Time`` / ``GPU_Memory``
    are the leaderboard CSV names; the snake_case variants are newer
    JSON emissions.
    """
    if not isinstance(metrics, dict):
        return {
            "avg_batch_time_ms": None,
            "gpu_memory_peak_mb": None,
            "n_params": None,
        }

    # Avg_Batch_Time is stored in seconds (e.g. 0.164 for 164 ms/batch).
    raw_bt = (
        metrics.get("Avg_Batch_Time")
        if "Avg_Batch_Time" in metrics
        else metrics.get("avg_batch_time")
    )
    bt_s = _coerce_float(raw_bt)
    avg_batch_time_ms = (
        round(bt_s * 1000.0, 3) if bt_s is not None else None
    )

    gpu_mem_mb = _coerce_float(
        metrics.get("GPU_Memory")
        if "GPU_Memory" in metrics
        else metrics.get("gpu_memory_peak_mb")
    )

    n_params = None
    for key in ("n_params", "Params", "model_params", "num_params"):
        if key in metrics:
            n_params = _coerce_int(metrics[key])
            if n_params is not None:
                break

    return {
        "avg_batch_time_ms": avg_batch_time_ms,
        "gpu_memory_peak_mb": gpu_mem_mb,
        "n_params": n_params,
    }


def _job_to_out(job: Job) -> JobOut:
    metrics: dict[str, Any] | None = None
    if job.metrics:
        try:
            metrics = json.loads(job.metrics)
        except json.JSONDecodeError:
            metrics = None
    if not isinstance(metrics, dict):
        metrics = None
    extras = _extract_job_extras(metrics)
    return JobOut(
        id=job.id,
        batch_id=job.batch_id,
        model=job.model,
        dataset=job.dataset,
        status=job.status,
        start_time=job.start_time,
        end_time=job.end_time,
        elapsed_s=job.elapsed_s,
        metrics=metrics,
        is_idle_flagged=bool(getattr(job, "is_idle_flagged", False)),
        **extras,
    )


async def _ensure_batch_visible(
    batch_id: str, user: User, session: AsyncSession,
    locale: SupportedLocale = "en-US",
) -> None:
    resolver = VisibilityResolver()
    if not await resolver.can_view_batch(user, batch_id, session):
        raise HTTPException(status_code=404, detail=tr(locale, "job.not_found"))


# ---------------------------------------------------------------------------
# Global jobs list — cross-batch, cross-host (#118)
# ---------------------------------------------------------------------------
#
# The ``/jobs`` page renders every job a user can see in a single paginated
# table, regardless of which batch / host / project the job lives in. The
# Dashboard's "Jobs running" / "Jobs failed (24h)" / "Jobs done (24h)" tiles
# deep-link here with a pre-filled ``status`` (and optional ``since=24h``).
#
# Visibility is reused 1-for-1 from VisibilityResolver: we resolve the set of
# batches the user can see, then filter Job rows by ``Job.batch_id IN (…)``.
# This deliberately matches the BatchList semantics (project-share viewer
# sees jobs in shared projects, batch-share viewer sees jobs in shared
# batches, admins see everything) — there is no separate ``job_share``
# table to consult.
#
# Pagination is offset/limit because the underlying ``order by start_time
# desc`` is index-backed and the absolute row count is small enough (low
# tens of thousands across the lifetime of an Argus instance) that we
# don't need a keyset cursor yet.


def _resolve_since(value: str | None) -> str | None:
    """Convert a ``since`` query value to an ISO-8601 string.

    Accepts either a literal ISO string (returned as-is) or a relative
    shorthand of the form ``<N><unit>`` where ``unit`` is ``m``/``h``/``d``.
    Returns ``None`` for a missing / unparseable value so the caller can
    omit the filter rather than 422 the request — consistent with the
    other ``since=`` consumers (``/api/batches``).
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    # Relative shorthand — e.g. "24h" → now - 24 hours.
    m = re.fullmatch(r"(\d+)([mhd])", s)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        delta = (
            timedelta(minutes=amount)
            if unit == "m"
            else timedelta(hours=amount)
            if unit == "h"
            else timedelta(days=amount)
        )
        cutoff = datetime.now(timezone.utc) - delta
        return (
            cutoff.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return s


class GlobalJobItem(BaseModel):
    """One row in the global ``GET /api/jobs`` response.

    Carries the JobOut payload plus the parent-batch context (project,
    host, batch_name) that the Jobs page table needs but Job rows don't
    store directly. These three are resolved server-side via a JOIN so
    the frontend doesn't have to fan-out N batch fetches.
    """

    model_config = ConfigDict(extra="forbid")

    job: JobOut
    project: str
    host: str | None = None
    batch_name: str | None = None


class GlobalJobListOut(BaseModel):
    """Paginated wrapper for ``GET /api/jobs``."""

    model_config = ConfigDict(extra="forbid")

    items: list[GlobalJobItem]
    total: int
    page: int
    page_size: int


@router.get("", response_model=GlobalJobListOut)
async def list_jobs_global(
    status: str | None = Query(default=None),
    project: str | None = Query(default=None),
    host: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    since: str | None = Query(
        default=None,
        description=(
            "ISO 8601 timestamp or relative shorthand "
            "(``24h`` / ``30m`` / ``7d``); filters by ``Job.start_time``."
        ),
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> GlobalJobListOut:
    """List jobs across all visible batches with filter + pagination.

    Visibility is delegated to :class:`VisibilityResolver` (scope=``all``):
    the user sees jobs from every batch they own, every batch shared
    directly to them, and every batch in a project shared to them.
    Admins see everything. Soft-deleted batches and jobs drop out.
    """
    # Cache key includes every filter so two filter combos don't collide.
    # Per-user prefix mirrors batches-list.
    since_iso = _resolve_since(since)
    key = (
        f"jobs-global:u{current.id}:"
        f"{status or '-'}:{project or '-'}:{host or '-'}:"
        f"{batch_id or '-'}:{since_iso or '-'}:{page}:{page_size}"
    )

    async def _load() -> GlobalJobListOut:
        resolver = VisibilityResolver()
        # Reuse the visibility query but only need batch ids + a tiny
        # context payload (project / host / name) to attach to each row.
        visible_q = await resolver.visible_batches_query(
            current, scope="all", db=session
        )
        if project is not None:
            visible_q = visible_q.where(Batch.project == project)
        if host is not None:
            visible_q = visible_q.where(Batch.host == host)
        if batch_id is not None:
            visible_q = visible_q.where(Batch.id == batch_id)

        batch_rows = (await session.execute(visible_q)).scalars().all()
        if not batch_rows:
            return GlobalJobListOut(
                items=[], total=0, page=page, page_size=page_size,
            )
        batch_ctx: dict[str, Batch] = {b.id: b for b in batch_rows}
        ids = list(batch_ctx.keys())

        # Job rows for the visible batches with the filter set applied.
        # Status filter is case-insensitive (Job.status free-form text).
        from sqlalchemy import func as _func

        base = (
            select(Job)
            .where(Job.batch_id.in_(ids))
            .where(Job.is_deleted.is_(False))
        )
        if status is not None:
            base = base.where(_func.lower(Job.status) == status.lower())
        if since_iso is not None:
            base = base.where(Job.start_time >= since_iso)

        # Total before pagination — single COUNT on the same predicate.
        count_stmt = select(_func.count()).select_from(base.subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)

        # Page slice. Newest first by start_time, NULLs last so a row
        # without a start_time doesn't crowd the top of every page.
        offset = (page - 1) * page_size
        page_stmt = (
            base.order_by(
                Job.start_time.desc().nullslast(),
                Job.id.asc(),
            )
            .offset(offset)
            .limit(page_size)
        )
        jobs = (await session.execute(page_stmt)).scalars().all()

        items: list[GlobalJobItem] = []
        for j in jobs:
            ctx = batch_ctx.get(j.batch_id)
            items.append(
                GlobalJobItem(
                    job=_job_to_out(j),
                    project=ctx.project if ctx is not None else "",
                    host=ctx.host if ctx is not None else None,
                    batch_name=ctx.name if ctx is not None else None,
                )
            )
        return GlobalJobListOut(
            items=items, total=total, page=page, page_size=page_size,
        )

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{batch_id}/{job_id}", response_model=JobOut)
async def get_job(
    batch_id: str,
    job_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> JobOut:
    """Return detail for a single job."""
    await _ensure_batch_visible(batch_id, current, session, locale)
    key = f"job:u{current.id}:{batch_id}:{job_id}"

    async def _load() -> JobOut:
        job = await session.get(Job, (job_id, batch_id))
        if job is None or getattr(job, "is_deleted", False):
            raise HTTPException(
                status_code=404, detail=tr(locale, "job.not_found")
            )
        return _job_to_out(job)

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{batch_id}/{job_id}/epochs", response_model=list[EpochPoint])
async def job_epochs(
    batch_id: str,
    job_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> list[EpochPoint]:
    """Return the ``job_epoch`` timeseries for this job, ordered by time."""
    await _ensure_batch_visible(batch_id, current, session, locale)
    key = f"job-epochs:u{current.id}:{batch_id}:{job_id}"

    async def _load() -> list[EpochPoint]:
        stmt = (
            select(Event)
            .where(Event.batch_id == batch_id)
            .where(Event.job_id == job_id)
            .where(Event.event_type == "job_epoch")
            .order_by(Event.timestamp.asc(), Event.id.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()
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
                # Malformed job_epoch row; skip rather than 500.
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

    return await _response_cache.get_or_compute(key, _load)


# ---------------------------------------------------------------------------
# Per-job log-line poll endpoint
# ---------------------------------------------------------------------------


class JobLogLineOut(BaseModel):
    """One ``log_line`` event row scoped to a single job.

    The shape mirrors :class:`backend.api.batches.LogLineOut` but adds
    ``id`` so callers can advance a ``since=<event_id>`` cursor without
    falling back to timestamp comparisons (which lose ordering when
    multiple lines share the same ISO second).

    ``event_id`` is the client-supplied UUID (v1.1 envelopes) — exposed
    so the frontend can dedup uniformly between this poll response and
    live SSE frames, where the DB ``id`` isn't available.
    """

    model_config = {"extra": "forbid"}

    id: int
    event_id: str | None
    ts: str
    job_id: str | None
    level: str
    line: str


@router.get(
    "/{batch_id}/{job_id}/log-lines", response_model=list[JobLogLineOut]
)
async def get_job_log_lines(
    batch_id: str,
    job_id: str,
    limit: int = Query(default=200, ge=1, le=2000),
    since: int | None = Query(
        default=None,
        description=(
            "Return only rows with ``Event.id > since``. Used by the "
            "JobDetail Logs tab to incrementally advance the buffer "
            "without re-fetching the full window."
        ),
    ),
    bust: str | None = Query(
        default=None,
        description="Pass any value to bypass the 10s response cache.",
    ),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> list[JobLogLineOut]:
    """Return the last ``limit`` ``log_line`` events for one job.

    This is the job-scoped sibling of
    ``GET /api/batches/{id}/log-lines``. The JobDetail Logs tab uses it
    for the initial buffer fill before opening the SSE stream — once the
    EventSource is connected, new lines arrive live, so this endpoint
    only fires on tab open + reconnect.

    Cached for 10s under :data:`default_cache`. Bypass with ``?bust=…``
    when freshness matters more than load (mirrors the batch endpoint).
    """
    await _ensure_batch_visible(batch_id, current, session, locale)
    # Validate the job exists in this batch so a typo doesn't return
    # an empty list and look like "no logs yet".
    job = await session.get(Job, (job_id, batch_id))
    if job is None or getattr(job, "is_deleted", False):
        raise HTTPException(
            status_code=404, detail=tr(locale, "job.not_found")
        )

    key = (
        f"job-log-lines:u{current.id}:{batch_id}:{job_id}:"
        f"{limit}:{since or '-'}"
    )
    if bust is not None:
        key = f"{key}:bust{bust}"

    async def _load() -> list[JobLogLineOut]:
        stmt = (
            select(Event)
            .where(Event.batch_id == batch_id)
            .where(Event.job_id == job_id)
            .where(Event.event_type == "log_line")
        )
        if since is not None:
            stmt = stmt.where(Event.id > since)
        # Pull the most recent ``limit`` rows so the caller always sees
        # the freshest tail; ``since`` filtering already trims ahead so
        # this is the upper bound, not the typical row count.
        stmt = stmt.order_by(Event.id.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

        out: list[JobLogLineOut] = []
        for ev in rows:
            data: dict[str, Any] = {}
            if ev.data:
                try:
                    parsed = json.loads(ev.data)
                    if isinstance(parsed, dict):
                        data = parsed
                except (json.JSONDecodeError, TypeError):
                    data = {}
            out.append(
                JobLogLineOut(
                    id=ev.id,
                    event_id=ev.event_id,
                    ts=ev.timestamp,
                    job_id=ev.job_id,
                    level=str(data.get("level", "info")),
                    line=str(data.get("line", data.get("message", ""))),
                )
            )
        return out

    return await _response_cache.get_or_compute(key, _load)


# ---------------------------------------------------------------------------
# Per-job ETA endpoint
# ---------------------------------------------------------------------------


class JobEtaOut(BaseModel):
    """Response for ``GET /api/jobs/{batch_id}/{job_id}/eta``."""

    model_config = {"extra": "forbid"}

    job_id: str
    elapsed_s: int
    epochs_done: int
    epochs_total: int
    avg_epoch_time_s: float | None
    eta_s: int | None
    eta_iso: str | None


def _extract_train_epochs(job: Job) -> int | None:
    """Pull ``train_epochs`` out of job.metrics JSON, or return None."""
    if not job.metrics:
        return None
    try:
        m = json.loads(job.metrics)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(m, dict):
        return None
    for key in ("train_epochs", "epochs", "n_epochs"):
        v = m.get(key)
        if isinstance(v, int) and v > 0:
            return v
    return None


@router.get("/{batch_id}/{job_id}/eta", response_model=JobEtaOut)
async def get_job_eta(
    batch_id: str,
    job_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> JobEtaOut:
    """Estimate remaining time for a single running job.

    Uses ``job_epoch`` event timestamps to compute the average per-epoch
    wall-clock time, then multiplies by ``epochs_total - epochs_done``.

    Returns ``eta_s=null`` while fewer than 2 epoch events have arrived
    ("warming up").

    Cached for 10s under the shared :data:`default_cache`. JobDetail
    polls this endpoint every 10s while a job is running, and BatchDetail
    has its own ``/jobs/eta-all`` polling — the per-job cache lets us
    survive a few overlapping pollers without re-running the timestamp
    scan on every call.
    """
    await _ensure_batch_visible(batch_id, current, session, locale)
    key = f"job-eta:u{current.id}:{batch_id}:{job_id}"

    async def _load() -> JobEtaOut:
        job = await session.get(Job, (job_id, batch_id))
        if job is None or getattr(job, "is_deleted", False):
            raise HTTPException(
                status_code=404, detail=tr(locale, "job.not_found")
            )

        # Fetch all job_epoch timestamps for this job, oldest first.
        rows = (
            await session.execute(
                select(Event.timestamp)
                .where(Event.batch_id == batch_id)
                .where(Event.job_id == job_id)
                .where(Event.event_type == "job_epoch")
                .order_by(Event.timestamp.asc(), Event.id.asc())
            )
        ).all()
        epoch_timestamps = [r[0] for r in rows if r[0] is not None]

        train_epochs = _extract_train_epochs(job)
        result = compute_job_eta(
            job_id=job_id,
            job_start_iso=job.start_time,
            train_epochs_config=train_epochs,
            epoch_timestamps=epoch_timestamps,
        )
        return JobEtaOut(
            job_id=result.job_id,
            elapsed_s=result.elapsed_s,
            epochs_done=result.epochs_done,
            epochs_total=result.epochs_total,
            avg_epoch_time_s=result.avg_epoch_time_s,
            eta_s=result.eta_s,
            eta_iso=result.eta_iso,
        )

    return await _response_cache.get_or_compute(key, _load)


# ---------------------------------------------------------------------------
# Soft delete (migration 021)
# ---------------------------------------------------------------------------


class BulkDeleteJobItem(BaseModel):
    """One ``(batch_id, job_id)`` pair in the bulk-delete payload."""

    model_config = {"extra": "forbid"}

    batch_id: str
    job_id: str


class BulkDeleteJobsIn(BaseModel):
    """Request body for ``POST /jobs/bulk-delete``.

    Capped at 500 items per request (v0.1.3 hardening). pydantic rejects
    oversize payloads with 422 before the per-row work starts.
    """

    model_config = {"extra": "forbid"}

    items: list[BulkDeleteJobItem] = Field(max_length=500)


class BulkDeleteSkip(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    reason: str


class BulkDeleteJobsOut(BaseModel):
    """Response shape for ``POST /jobs/bulk-delete``."""

    model_config = {"extra": "forbid"}

    deleted: list[str]
    skipped: list[BulkDeleteSkip]


@router.post("/bulk-delete", response_model=BulkDeleteJobsOut, status_code=200)
async def bulk_delete_jobs(
    payload: BulkDeleteJobsIn,
    request: Request,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> BulkDeleteJobsOut:
    """Soft-delete many jobs in one round-trip.

    Per-row checks: parent batch exists and is visible; caller is the
    owner or admin; job hasn't already been deleted. ``deleted`` /
    ``skipped`` carry the per-id verdict so the UI can render a partial
    success toast (`8/10 deleted`).
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="items must be non-empty")

    deleted: list[str] = []
    skipped: list[BulkDeleteSkip] = []
    audit = get_audit_service()
    ip = request.client.host if request.client else None
    bust_targets: set[str] = set()

    for item in payload.items:
        composite_id = f"{item.batch_id}/{item.job_id}"
        batch = await session.get(Batch, item.batch_id)
        if batch is None or batch.is_deleted:
            skipped.append(BulkDeleteSkip(id=composite_id, reason="batch_not_found"))
            continue
        if not current.is_admin and batch.owner_id != current.id:
            skipped.append(BulkDeleteSkip(id=composite_id, reason="not_owner"))
            continue
        job = await session.get(Job, (item.job_id, item.batch_id))
        if job is None:
            skipped.append(BulkDeleteSkip(id=composite_id, reason="not_found"))
            continue
        if getattr(job, "is_deleted", False):
            skipped.append(BulkDeleteSkip(id=composite_id, reason="already_deleted"))
            continue
        # Safety guard (v0.1.3): partition active jobs into ``skipped``
        # so the rest of the bulk call still proceeds.
        job_status = (job.status or "").lower()
        if job_status in {"running", "pending"}:
            skipped.append(
                BulkDeleteSkip(id=composite_id, reason=job_status)
            )
            continue
        job.is_deleted = True
        deleted.append(composite_id)
        bust_targets.add(item.batch_id)
        audit.log_background(
            action="job_deleted",
            user_id=current.id,
            target_type="job",
            target_id=composite_id,
            metadata={
                "batch_id": item.batch_id,
                "job_id": item.job_id,
                "via": "bulk",
            },
            ip=ip,
        )

    if deleted:
        await session.commit()
        from backend.api.shares import _bust_batch_cache_for_user

        for bid in bust_targets:
            _bust_batch_cache_for_user(bid, current.id)

    return BulkDeleteJobsOut(deleted=deleted, skipped=skipped)


@router.delete("/{batch_id}/{job_id}", status_code=200)
async def delete_job(
    batch_id: str,
    job_id: str,
    request: Request,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> dict:
    """Soft-delete a single job.

    Sets ``Job.is_deleted=True``; the row stays so the audit trail and
    per-batch counters keep their integrity. Owner of the parent batch
    or admin only.
    """
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.is_deleted:
        raise HTTPException(
            status_code=404, detail=tr(locale, "batch.not_found")
        )
    if not current.is_admin and batch.owner_id != current.id:
        raise HTTPException(
            status_code=403,
            detail=tr(locale, "share.batch.owner_only"),
        )

    job = await session.get(Job, (job_id, batch_id))
    if job is None or getattr(job, "is_deleted", False):
        raise HTTPException(
            status_code=404, detail=tr(locale, "job.not_found")
        )

    # Safety guard (v0.1.3): refuse deletion while the job is still
    # actively reporting. The user can either wait for the job to
    # finish naturally or stop the parent batch and let the reporter
    # cooperatively flag the job as failed/stopped first.
    job_status = (job.status or "").lower()
    if job_status in {"running", "pending"}:
        raise HTTPException(
            status_code=409,
            detail=tr(locale, "job.delete_blocked_running"),
        )

    job.is_deleted = True
    await session.commit()

    # Bust per-user cache for this batch so the next read reflects the
    # deletion immediately. Imported here to avoid a circular import
    # with the shares router at module load time.
    from backend.api.shares import _bust_batch_cache_for_user

    _bust_batch_cache_for_user(batch_id, current.id)

    get_audit_service().log_background(
        action="job_deleted",
        user_id=current.id,
        target_type="job",
        target_id=f"{batch_id}/{job_id}",
        metadata={"batch_id": batch_id, "job_id": job_id},
        ip=(request.client.host if request.client else None),
    )

    return {"status": "deleted", "batch_id": batch_id, "job_id": job_id}
