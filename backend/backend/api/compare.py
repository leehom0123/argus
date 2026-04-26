"""``/api/compare`` — side-by-side batch comparison.

The endpoint accepts 2-N batch ids (N up to :data:`MAX_COMPARE_BATCHES`,
currently 32 per issue #19) via comma-separated query param and returns
one column per batch with its jobs + headline metrics. A
``metric_union`` helper list contains every metric name observed across
the batches so the UI can render a consistent column set.

CSV export (``/api/compare/export.csv``) streams the same data as a
matrix-shaped CSV: rows = (batch_id, model, dataset, metric), cols
= one per metric value.

Visibility: every batch in the list must be visible to the caller;
invisible ids 404. We deliberately do NOT silently drop — callers
should know if they asked for something they can't see.
"""
from __future__ import annotations

import csv
import io
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, Job, User
from backend.schemas.compare import (
    MAX_COMPARE_BATCHES,
    CompareBatchColumn,
    CompareJobMetric,
    CompareOut,
)
from backend.services.visibility import VisibilityResolver
from backend.utils.response_cache import default_cache as _response_cache

router = APIRouter(prefix="/api/compare", tags=["compare"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_batch_list(raw: str) -> list[str]:
    """Parse ``batches=a,b,c`` into a de-duplicated preserved-order list."""
    out: list[str] = []
    for item in raw.split(","):
        s = item.strip()
        if s and s not in out:
            out.append(s)
    return out


def _safe_metrics(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _validate_and_resolve_batches(
    raw: str, user: User, db: AsyncSession,
    locale: SupportedLocale = "en-US",
) -> list[Batch]:
    ids = _parse_batch_list(raw)
    if len(ids) < 2:
        raise HTTPException(
            status_code=400,
            detail=tr(locale, "compare.too_few"),
        )
    if len(ids) > MAX_COMPARE_BATCHES:
        raise HTTPException(
            status_code=400,
            detail=tr(locale, "compare.too_many", max=MAX_COMPARE_BATCHES, count=len(ids)),
        )

    resolver = VisibilityResolver()
    resolved: list[Batch] = []
    for bid in ids:
        if not await resolver.can_view_batch(user, bid, db):
            raise HTTPException(
                status_code=404,
                detail=tr(locale, "compare.batch.not_found", batch_id=bid),
            )
        batch = await db.get(Batch, bid)
        # can_view_batch rules out None/deleted already.
        resolved.append(batch)  # type: ignore[arg-type]
    return resolved


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=CompareOut)
async def compare_batches(
    batches: str = Query(
        ...,
        description=(
            "Comma-separated batch ids (2 to MAX_COMPARE_BATCHES, "
            "currently 32)."
        ),
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> CompareOut:
    """Return side-by-side data for 2 to :data:`MAX_COMPARE_BATCHES` batches.

    Each batch column carries the owning batch row, its jobs, and a
    ``best_metric`` dict (lowest MSE across the batch's done jobs). Per
    issue #19 the endpoint accepts up to 32 batches so full sweeps
    (e.g. the 32-combination CC ablation) can be compared in one
    request. The frontend can either render all columns inline or
    paginate client-side; the response shape is unchanged from the
    original N<=4 contract.

    Cached: visibility-resolved compare results are placed behind
    :data:`default_cache` keyed by ``(user_id, sorted_batch_ids)``.
    The 10s TTL is more than enough to absorb the dashboard / Compare
    page tab flips that issue the same query twice in quick succession,
    while still being short enough that operators see fresh metrics
    without an explicit Refresh click. Visibility check happens before
    the cache key, so a user can never observe another user's cached
    payload.
    """
    # Visibility check (and 404 on hidden batches) runs every call —
    # the cache layer never sees an unresolved request.
    rows = await _validate_and_resolve_batches(batches, user, db, locale)

    # Sort ids so ``a,b`` and ``b,a`` collapse onto the same cache slot.
    sorted_ids = ",".join(sorted(b.id for b in rows))
    key = f"compare:u{user.id}:{sorted_ids}"

    async def _load() -> CompareOut:
        metric_union: set[str] = set()
        columns: list[CompareBatchColumn] = []

        # Perf (Team Perf): one IN-list query across every batch instead
        # of N sequential queries. With N=32 and ~10 jobs each, the batch
        # fetch drops from 32 → 1 round-trips and 32× planner overhead.
        batch_ids = [b.id for b in rows]
        jobs_by_batch: dict[str, list[Job]] = {bid: [] for bid in batch_ids}
        if batch_ids:
            all_jobs = (
                await db.execute(
                    select(Job)
                    .where(Job.batch_id.in_(batch_ids))
                    .order_by(Job.start_time.asc().nullslast(), Job.id.asc())
                )
            ).scalars().all()
            for j in all_jobs:
                jobs_by_batch.setdefault(j.batch_id, []).append(j)

        for batch in rows:
            jobs = jobs_by_batch.get(batch.id, [])

            job_metrics: list[CompareJobMetric] = []
            best_mse: float | None = None
            best_metric_name = "MSE"
            for job in jobs:
                parsed = _safe_metrics(job.metrics)
                if parsed:
                    metric_union.update(parsed.keys())
                    mse = parsed.get("MSE")
                    if isinstance(mse, (int, float)):
                        if best_mse is None or mse < best_mse:
                            best_mse = float(mse)
                job_metrics.append(
                    CompareJobMetric(
                        job_id=job.id,
                        model=job.model,
                        dataset=job.dataset,
                        status=job.status,
                        elapsed_s=job.elapsed_s,
                        metrics=parsed,
                    )
                )

            best_metric: dict | None = (
                {"name": best_metric_name, "value": best_mse}
                if best_mse is not None
                else None
            )
            columns.append(
                CompareBatchColumn(
                    batch_id=batch.id,
                    project=batch.project,
                    status=batch.status,
                    n_total=batch.n_total,
                    n_done=batch.n_done or 0,
                    n_failed=batch.n_failed or 0,
                    start_time=batch.start_time,
                    end_time=batch.end_time,
                    owner_id=batch.owner_id,
                    jobs=job_metrics,
                    best_metric=best_metric,
                )
            )

        return CompareOut(
            batches=columns,
            metric_union=sorted(metric_union),
        )

    return await _response_cache.get_or_compute(key, _load)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def _format_metric(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _csv_stream(rows: list[list[str]], filename: str) -> StreamingResponse:
    async def _iter() -> AsyncIterator[bytes]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in rows:
            writer.writerow(row)
            data = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            if data:
                yield data.encode("utf-8")

    return StreamingResponse(
        _iter(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@router.get("/export.csv")
async def export_compare_csv(
    batches: str = Query(..., description="Comma-separated batch ids (2-4)."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> StreamingResponse:
    """CSV export of the same data ``GET /api/compare`` returns."""
    resolved = await _validate_and_resolve_batches(batches, user, db, locale)

    # Pre-scan to collect the metric column union (stable column order
    # regardless of batch order).
    metric_union: list[str] = []
    seen: set[str] = set()
    jobs_by_batch: dict[str, list[Job]] = {}
    for batch in resolved:
        jobs = (
            await db.execute(
                select(Job)
                .where(Job.batch_id == batch.id)
                .order_by(Job.id.asc())
            )
        ).scalars().all()
        jobs_by_batch[batch.id] = jobs
        for job in jobs:
            m = _safe_metrics(job.metrics) or {}
            for k in m.keys():
                if k not in seen:
                    seen.add(k)
                    metric_union.append(k)
    metric_union.sort()

    header = [
        "batch_id", "project", "job_id", "model", "dataset",
        "status", "elapsed_s", *metric_union,
    ]
    rows: list[list[str]] = [header]
    for batch in resolved:
        for job in jobs_by_batch.get(batch.id, []):
            metrics = _safe_metrics(job.metrics) or {}
            rows.append([
                batch.id,
                batch.project,
                job.id,
                job.model or "",
                job.dataset or "",
                job.status or "",
                str(job.elapsed_s) if job.elapsed_s is not None else "",
                *[_format_metric(metrics.get(k)) for k in metric_union],
            ])

    return _csv_stream(rows, "compare.csv")
