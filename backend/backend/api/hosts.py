"""Per-host resource timeseries endpoint.

Adds ``GET /api/hosts/{host}/timeseries`` which returns bucketed
GPU/RAM/CPU usage stacked by ``batch_id`` so the frontend can render
a stacked-area chart showing how the host's resources are split among
concurrent batches over time.

Depends on PR-A (migration 008) for per-process ``proc_*`` columns on
``resource_snapshot``. The aggregation code uses ``getattr`` fallbacks
so this endpoint is forward-compatible while PR-A is in flight.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from backend.db import get_session
from backend.deps import get_current_user
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import HostMeta, ResourceSnapshot, User
from backend.schemas.hosts import HostTimeseriesOut
from backend.services.audit import get_audit_service
from backend.services.dashboard import DashboardService
from backend.utils.response_cache import default_cache as _response_cache
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/hosts", tags=["hosts"])

_svc = DashboardService()


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@router.get("/{host}/timeseries", response_model=HostTimeseriesOut)
async def host_timeseries(
    host: str,
    metric: str = Query(
        default="gpu_mem_mb",
        description=(
            "Metric to aggregate: gpu_mem_mb | gpu_util_pct | "
            "cpu_util_pct | ram_mb"
        ),
    ),
    since: str | None = Query(
        default=None,
        description=(
            "Window start as ISO 8601 timestamp or relative string "
            "like 'now-2h', 'now-30m'. Defaults to 'now-1h'."
        ),
    ),
    bucket_seconds: int = Query(
        default=60,
        ge=10,
        le=3600,
        description="Bucket width in seconds (10–3600).",
    ),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HostTimeseriesOut:
    """Return time-bucketed resource usage for *host*, stacked by batch.

    Each bucket holds the host-level ``total`` value (averaged within
    the bucket) plus a ``by_batch`` dict mapping ``batch_id`` →
    per-process sum for that bucket.

    ``host_total_capacity`` is the physical limit for the metric:
    * ``gpu_mem_mb``   → ``gpu_mem_total_mb`` of the latest snapshot
    * ``ram_mb``       → ``ram_total_mb`` of the latest snapshot
    * ``*_pct``        → 100

    Returns **404** when the host has never posted a snapshot.
    Returns **200** with an empty ``buckets`` list when the host exists
    but has no data in the requested window.
    """
    # Demo hosts are invisible to every authenticated caller (2026-04-24
    # flip). 404 matches the "host never posted" branch so we don't
    # leak that a demo host exists under a different surface.
    demo_hosts = await _svc._demo_host_names(session)
    if host in demo_hosts:
        raise HTTPException(status_code=404, detail=f"Host '{host}' not found")
    # Soft-deleted hosts (migration 021) — admin can re-enable by
    # toggling the meta row; until then the host is hidden from the UI.
    meta = await session.get(HostMeta, host)
    if meta is not None and meta.is_deleted:
        raise HTTPException(status_code=404, detail=f"Host '{host}' not found")

    # Host timeseries payloads are the same for every authenticated
    # caller at a given (host, metric, since, bucket_seconds), so we
    # share the entry across users. 10s staleness is fine: the chart
    # already uses 60s buckets by default.
    key = (
        f"host_timeseries:shared:{host}:{metric}:"
        f"{since or '-'}:{bucket_seconds}"
    )

    async def _load() -> HostTimeseriesOut:
        result = await _svc.host_resource_timeseries(
            host=host,
            db=session,
            metric=metric,
            since=since,
            bucket_seconds=bucket_seconds,
        )
        if result is None:
            raise HTTPException(
                status_code=404, detail=f"Host '{host}' not found"
            )
        return HostTimeseriesOut.model_validate(result)

    return await _response_cache.get_or_compute(key, _load)


# ---------------------------------------------------------------------------
# Soft delete (migration 021) — admin only
# ---------------------------------------------------------------------------


@router.delete("/{host}", status_code=200)
async def delete_host(
    host: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> dict:
    """Soft-delete a host.

    Hosts aren't a first-class entity in the schema — they're derived
    from :attr:`ResourceSnapshot.host`. The delete endpoint upserts a
    :class:`HostMeta` row with ``is_deleted=True`` so the host stops
    appearing in list / detail / timeseries surfaces. Snapshot rows
    are intentionally retained: older batches may still link back to
    them and breaking that join would corrupt historical analytics.

    Admin only.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail=tr(locale, "admin.privileges_required"),
        )

    # Safety guard (v0.1.3): refuse to hide a host that has reported a
    # resource snapshot in the last 10 minutes — jobs are likely still
    # mid-flight and would keep ingesting against a hidden host.
    active_cutoff_iso = (
        (datetime.now(timezone.utc) - timedelta(minutes=10))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    last_seen = (
        await session.execute(
            select(func.max(ResourceSnapshot.timestamp))
            .where(ResourceSnapshot.host == host)
        )
    ).scalar_one_or_none()
    if last_seen is not None and last_seen >= active_cutoff_iso:
        raise HTTPException(
            status_code=409,
            detail=tr(locale, "host.delete_blocked_active"),
        )

    now = _utcnow_iso()
    meta = await session.get(HostMeta, host)
    if meta is None:
        meta = HostMeta(
            host=host,
            is_deleted=True,
            deleted_at=now,
            deleted_by_user_id=user.id,
            hidden_at=now,
        )
        session.add(meta)
    else:
        meta.is_deleted = True
        meta.deleted_at = now
        meta.deleted_by_user_id = user.id
        meta.hidden_at = now

    await session.commit()

    get_audit_service().log_background(
        action="host_deleted",
        user_id=user.id,
        target_type="host",
        target_id=host,
        metadata={"host": host},
        ip=(request.client.host if request.client else None),
    )

    return {"status": "deleted", "host": host}
