"""Batch list + detail endpoints.

Routes mounted under ``/api/batches``:

* ``GET    /api/batches``                       — paginated list (visibility-filtered)
* ``GET    /api/batches/compact``               — bulk hydrated cards for grids
* ``GET    /api/batches/{id}``                  — full detail
* ``GET    /api/batches/{id}/health``           — stalled / failure summary
* ``GET    /api/batches/{id}/eta``              — EMA-based remaining seconds
* ``GET    /api/batches/{id}/export.csv``       — per-job CSV export
* ``POST   /api/batches/{id}/stop``             — owner/admin; sets status='stopping'
* ``GET    /api/batches/{id}/stop-requested``   — reporter polling endpoint
* ``POST   /api/batches/rerun``                 — relaunch a batch with overrides
* ``DELETE /api/batches/{id}``                  — soft-delete (owner/admin)
* ``DELETE /api/batches``                       — bulk soft-delete

Soft delete sets ``Batch.is_deleted=True``; every list/detail surface
already filters on the column so deleted rows drop out of the UI but
remain for audit + retention. ``VisibilityResolver.can_edit_batch`` is
exercised by the share-permission tests; the corresponding ``PATCH``
endpoint for ``{name, tag}`` is parked until product asks for it.
"""
from __future__ import annotations

import csv
import io
import json
import uuid as _uuid_mod
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_session
from backend.deps import get_current_user
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, Event, Job, ResourceSnapshot, User
from backend.schemas import BatchOut, JobOut, ResourceSnapshotOut
from backend.schemas.batches_compact import (
    BatchCompactItem,
    BatchCompactListOut,
    JobEpochLatestItem,
)
from backend.schemas.projects import BatchEtaOut, BatchHealthOut
from backend.services.audit import get_audit_service
from backend.services.eta import compute_job_eta, ema_eta
from backend.services.executor import (
    BatchNotFound as ExecutorBatchNotFound,
    InvalidSourceState,
    get_executor,
)
from backend.services.feature_flags import get_flag
from backend.services.health import batch_health
from backend.services.visibility import VisibilityResolver
from backend.utils.ratelimit import get_default_bucket
from backend.utils.response_cache import default_cache as _response_cache

# ---------------------------------------------------------------------------
# Hot-read caching
# ---------------------------------------------------------------------------
#
# The ``/batches`` page renders N BatchCompactCard components that each fan
# out to 4 parallel GETs (get_batch + list_batch_jobs + batch_epochs_latest +
# batch_resources). With 11 batches that's 44 DB-touching requests every
# render. We wrap the hot read paths with the shared :data:`default_cache`
# (10s TTL) to collapse those into one loader run per (user, batch, params)
# key. The bulk ``jobs/eta-all`` endpoint previously shipped its own bespoke
# module-level 10s dict; it's been migrated to the shared cache for
# consistency so per-request cache busts can live in one place.

router = APIRouter(prefix="/api/batches", tags=["batches"])


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _job_to_out(job: Job) -> JobOut:
    # Delegate to the ``jobs.py`` helper so the #21 hover-card extras
    # (avg_batch_time_ms / gpu_memory_peak_mb / n_params) and the #13
    # idle flag are populated identically on both GET /api/batches/{id}/jobs
    # and /api/jobs/{id}.
    from backend.api.jobs import _job_to_out as _jobs_job_to_out

    return _jobs_job_to_out(job)


def _normalise_git_remote(raw: str | None) -> str | None:
    """Convert a raw ``git_remote`` string to a browsable HTTPS URL.

    Accepts the usual set of reporter-collected formats:

      * ``git@github.com:user/repo.git`` — SSH
      * ``ssh://git@github.com/user/repo.git``
      * ``https://github.com/user/repo.git`` — HTTPS already
      * ``https://github.com/user/repo`` — no ``.git`` suffix

    Returns the stripped HTTPS form (no trailing ``.git``). Anything
    that doesn't look like a recognised remote is returned as-is so
    the frontend can decide whether to render it verbatim. ``None`` →
    ``None``.
    """
    if not raw or not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    # SSH form: ``git@host:path``
    if value.startswith("git@") and ":" in value:
        host, _, path = value[4:].partition(":")
        if host and path:
            value = f"https://{host}/{path}"
    elif value.startswith("ssh://"):
        # ssh://git@host/path  → https://host/path
        rest = value[len("ssh://"):]
        if rest.startswith("git@"):
            rest = rest[len("git@"):]
        value = f"https://{rest}"
    # Strip a trailing ``.git`` so the URL concatenates cleanly with
    # ``/commit/<sha>`` on the frontend side.
    if value.endswith(".git"):
        value = value[: -len(".git")]
    return value


def _enrich_env_snapshot(snap: dict | None) -> dict | None:
    """Add ``git_sha_short`` + ``git_remote_url`` to the snapshot dict.

    Both are injected best-effort:
      * ``git_sha_short`` = first 8 chars of ``git_sha`` when non-empty
      * ``git_remote_url`` = normalised ``git_remote`` (SSH / HTTPS /
        plain) or ``None`` when the reporter didn't capture one

    Returning ``None`` for missing fields (rather than omitting them)
    lets the frontend gracefully hide the SHA chip / GitHub link.
    """
    if not isinstance(snap, dict):
        return snap
    # Don't mutate the JSON-loaded dict reference in case it's reused
    enriched = dict(snap)
    git_sha = enriched.get("git_sha")
    if isinstance(git_sha, str) and git_sha.strip():
        enriched["git_sha_short"] = git_sha.strip()[:8]
    else:
        enriched["git_sha_short"] = None
    enriched["git_remote_url"] = _normalise_git_remote(
        enriched.get("git_remote")
    )
    return enriched


@router.get("", response_model=list[BatchOut])
async def list_batches(
    user: str | None = None,
    project: str | None = None,
    status: str | None = None,
    scope: Literal["mine", "shared", "all", "public"] = Query(
        default="all",
        description=(
            "Visibility filter: 'mine' = I own, 'shared' = shared to "
            "me, 'all' = mine ∪ shared (admins see everything), "
            "'public' = batches with a public share."
        ),
    ),
    since: str | None = Query(
        default=None,
        description="ISO 8601 timestamp; filter batches started on or after.",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[BatchOut]:
    """List batches with optional filters, newest first by start_time."""
    # Visibility-filtered list depends on (user, scope, user-filter, project,
    # status, since, limit). Key by user id so a per-user visibility filter
    # doesn't leak across callers.
    key = (
        f"batches-list:u{current.id}:{scope}:"
        f"{user or '-'}:{project or '-'}:{status or '-'}:"
        f"{since or '-'}:{limit}"
    )

    async def _load() -> list[BatchOut]:
        resolver = VisibilityResolver()
        stmt = await resolver.visible_batches_query(current, scope, db=session)
        if user is not None:
            stmt = stmt.where(Batch.user == user)
        if project is not None:
            stmt = stmt.where(Batch.project == project)
        if status is not None:
            stmt = stmt.where(Batch.status == status)
        if since is not None:
            stmt = stmt.where(Batch.start_time >= since)
        stmt = stmt.order_by(Batch.start_time.desc().nullslast()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        return [BatchOut.model_validate(r) for r in rows]

    return await _response_cache.get_or_compute(key, _load)


# ---------------------------------------------------------------------------
# Bulk compact list — the real fan-out fix for /batches page
# ---------------------------------------------------------------------------
#
# The ``/batches`` page used to render one card per visible batch and each
# card fanned out to four GETs (``/batches/{id}``, ``/batches/{id}/jobs``,
# ``/batches/{id}/epochs/latest``, ``/batches/{id}/resources``). Even after
# the 10s TTL cache per endpoint, the N×4 fan-out still hit the backend
# pool hard on first paint. This endpoint assembles everything a
# ``BatchCompactCard`` needs in **4 queries total** regardless of N:
#
#   1. visible batches   (VisibilityResolver, same filters as list_batches)
#   2. jobs              (SELECT … WHERE batch_id IN (…))
#   3. latest job_epoch  (per job, one pass ordered ASC; Python reduce)
#   4. resource snapshot (window-function, ROW_NUMBER by batch_id, cap N)
#
# Must be registered BEFORE ``/{batch_id}`` so ``/compact`` doesn't resolve
# to "a batch literally named compact".
@router.get("/compact", response_model=BatchCompactListOut)
async def list_batches_compact(
    user: str | None = None,
    project: str | None = None,
    status: str | None = None,
    scope: Literal["mine", "shared", "all", "public"] = Query(
        default="all",
        description="Visibility filter — same semantics as list_batches.",
    ),
    since: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    resource_limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Max resource snapshots returned per batch (newest first).",
    ),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchCompactListOut:
    """Bulk version of list_batches — returns BatchCompactItem[] in one call.

    Each item contains:

    * ``batch``         — BatchOut (minus ``env_snapshot`` enrichment,
                          which list-view cards don't use)
    * ``jobs``          — every Job row for the batch
    * ``epochs_latest`` — per-job latest ``job_epoch`` event + a 20-point
                          val_loss trace
    * ``resources``     — up to ``resource_limit`` newest ResourceSnapshot
                          rows for the batch (via window function)

    Cached for 10s under a per-user + per-filter key.
    """
    key = (
        f"batches-compact:u{current.id}:{scope}:"
        f"{user or '-'}:{project or '-'}:{status or '-'}:"
        f"{since or '-'}:{limit}:{resource_limit}"
    )

    async def _load() -> BatchCompactListOut:
        # -------- 1. visible batches --------------------------------------
        resolver = VisibilityResolver()
        stmt = await resolver.visible_batches_query(current, scope, db=session)
        if user is not None:
            stmt = stmt.where(Batch.user == user)
        if project is not None:
            stmt = stmt.where(Batch.project == project)
        if status is not None:
            stmt = stmt.where(Batch.status == status)
        if since is not None:
            stmt = stmt.where(Batch.start_time >= since)
        stmt = stmt.order_by(Batch.start_time.desc().nullslast()).limit(limit)
        batch_rows = (await session.execute(stmt)).scalars().all()

        if not batch_rows:
            return BatchCompactListOut(batches=[])

        batch_ids = [b.id for b in batch_rows]

        # -------- 2. jobs for all visible batches -------------------------
        # Single IN-list query; delegate per-job JobOut conversion to the
        # ``jobs.py`` helper so flags (is_idle_flagged) and hover-card
        # extras match every other surface.
        from backend.api.jobs import _job_to_out as _jobs_job_to_out

        jobs_stmt = (
            select(Job)
            .where(Job.batch_id.in_(batch_ids))
            .where(Job.is_deleted.is_(False))
            .order_by(Job.batch_id.asc(), Job.start_time.asc().nullslast(), Job.id.asc())
        )
        all_jobs = (await session.execute(jobs_stmt)).scalars().all()
        jobs_by_batch: dict[str, list[JobOut]] = {}
        for j in all_jobs:
            jobs_by_batch.setdefault(j.batch_id, []).append(_jobs_job_to_out(j))

        # -------- 3. latest job_epoch per job -----------------------------
        # One pass over all job_epoch events for the visible batches,
        # ordered ASC by timestamp so the last dict-assign wins for
        # "latest" and the trace builds chronologically.
        epoch_stmt = (
            select(Event)
            .where(Event.batch_id.in_(batch_ids))
            .where(Event.event_type == "job_epoch")
            .where(Event.job_id.is_not(None))
            .order_by(Event.timestamp.asc(), Event.id.asc())
        )
        epoch_rows = (await session.execute(epoch_stmt)).scalars().all()

        # {batch_id: {job_id: {"latest": dict, "trace": [float]}}}
        epoch_state: dict[str, dict[str, dict[str, Any]]] = {}
        for ev in epoch_rows:
            data: dict[str, Any] = {}
            if ev.data:
                try:
                    data = json.loads(ev.data)
                except (json.JSONDecodeError, TypeError):
                    data = {}
            bkt = epoch_state.setdefault(ev.batch_id, {})
            slot = bkt.setdefault(ev.job_id or "", {"latest": {}, "trace": []})
            slot["latest"] = data
            vl = data.get("val_loss")
            if vl is not None:
                try:
                    slot["trace"].append(float(vl))
                except (TypeError, ValueError):
                    pass

        epochs_by_batch: dict[str, list[JobEpochLatestItem]] = {}
        for bid, jobs_map in epoch_state.items():
            items: list[JobEpochLatestItem] = []
            for jid, state in jobs_map.items():
                d = state["latest"]
                trace = state["trace"]
                items.append(
                    JobEpochLatestItem(
                        job_id=jid,
                        epoch=int(d.get("epoch", 0)),
                        train_loss=(
                            float(d["train_loss"])
                            if d.get("train_loss") is not None else None
                        ),
                        val_loss=(
                            float(d["val_loss"])
                            if d.get("val_loss") is not None else None
                        ),
                        lr=(
                            float(d["lr"]) if d.get("lr") is not None else None
                        ),
                        val_loss_trace=trace[-20:],
                    )
                )
            epochs_by_batch[bid] = items

        # -------- 4. newest N resource snapshots per batch ---------------
        # Use ROW_NUMBER() OVER (PARTITION BY batch_id ORDER BY ts DESC)
        # then filter rn <= resource_limit so we keep the work server-side.
        rn = (
            func.row_number()
            .over(
                partition_by=ResourceSnapshot.batch_id,
                order_by=ResourceSnapshot.timestamp.desc(),
            )
            .label("rn")
        )
        rs_inner = (
            select(ResourceSnapshot, rn)
            .where(ResourceSnapshot.batch_id.in_(batch_ids))
        ).subquery()
        rs_stmt = (
            select(ResourceSnapshot)
            .join(rs_inner, ResourceSnapshot.id == rs_inner.c.id)
            .where(rs_inner.c.rn <= resource_limit)
            .order_by(
                ResourceSnapshot.batch_id.asc(),
                ResourceSnapshot.timestamp.desc(),
            )
        )
        rs_rows = (await session.execute(rs_stmt)).scalars().all()
        res_by_batch: dict[str, list[ResourceSnapshotOut]] = {}
        for r in rs_rows:
            res_by_batch.setdefault(r.batch_id or "", []).append(
                ResourceSnapshotOut.model_validate(r)
            )

        # -------- 5. assemble items in batch-list order -------------------
        items: list[BatchCompactItem] = []
        for b in batch_rows:
            items.append(
                BatchCompactItem(
                    batch=BatchOut.model_validate(b),
                    jobs=jobs_by_batch.get(b.id, []),
                    epochs_latest=epochs_by_batch.get(b.id, []),
                    resources=res_by_batch.get(b.id, []),
                )
            )
        return BatchCompactListOut(batches=items)

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{batch_id}", response_model=BatchOut)
async def get_batch(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> BatchOut:
    """Return detail for a single batch with live aggregated counters."""
    key = f"batch:u{current.id}:{batch_id}"

    async def _load() -> BatchOut:
        batch = await session.get(Batch, batch_id)
        if batch is None or batch.is_deleted:
            raise HTTPException(
                status_code=404, detail=tr(locale, "batch.not_found")
            )
        resolver = VisibilityResolver()
        if not await resolver.can_view_batch(current, batch_id, session):
            raise HTTPException(
                status_code=404, detail=tr(locale, "batch.not_found")
            )
        # Refresh counters from actual job rows so the response matches reality
        # even if an out-of-order event skipped the normal recompute path.
        result = await session.execute(
            select(Job.status)
            .where(Job.batch_id == batch_id)
            .where(Job.is_deleted.is_(False))
        )
        statuses = [r[0] for r in result.all()]
        out = BatchOut.model_validate(batch)
        out.n_done = sum(1 for s in statuses if s and s.lower() == "done")
        out.n_failed = sum(1 for s in statuses if s and s.lower() == "failed")
        # Decode env_snapshot_json into a dict for the ReproChipRow UI.
        # #18: enrich with derived ``git_sha_short`` + ``git_remote_url``
        # so the frontend chip can build ``{remote_url}/commit/{sha}``
        # without re-parsing git semantics in JS.
        raw = getattr(batch, "env_snapshot_json", None)
        if raw:
            try:
                parsed = json.loads(raw)
                out.env_snapshot = _enrich_env_snapshot(parsed)
            except (json.JSONDecodeError, TypeError):
                out.env_snapshot = None
        return out

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{batch_id}/jobs", response_model=list[JobOut])
async def list_batch_jobs(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> list[JobOut]:
    """List all jobs belonging to one batch."""
    key = f"batch-jobs:u{current.id}:{batch_id}"

    async def _load() -> list[JobOut]:
        batch = await session.get(Batch, batch_id)
        if batch is None or batch.is_deleted:
            raise HTTPException(
                status_code=404, detail=tr(locale, "batch.not_found")
            )
        resolver = VisibilityResolver()
        if not await resolver.can_view_batch(current, batch_id, session):
            raise HTTPException(
                status_code=404, detail=tr(locale, "batch.not_found")
            )
        stmt = (
            select(Job)
            .where(Job.batch_id == batch_id)
            .where(Job.is_deleted.is_(False))
            .order_by(Job.start_time.asc().nullslast(), Job.id.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [_job_to_out(j) for j in rows]

    return await _response_cache.get_or_compute(key, _load)


# ---------------------------------------------------------------------------
# BACKEND-E: health / eta / export.csv
# ---------------------------------------------------------------------------


_STANDARD_METRICS = ("MSE", "MAE", "RMSE", "R2", "PCC", "MAPE")


async def _require_visible_batch(
    batch_id: str, user: User, session: AsyncSession,
    locale: SupportedLocale = "en-US",
) -> Batch:
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.is_deleted:
        raise HTTPException(status_code=404, detail=tr(locale, "batch.not_found"))
    resolver = VisibilityResolver()
    if not await resolver.can_view_batch(user, batch_id, session):
        raise HTTPException(status_code=404, detail=tr(locale, "batch.not_found"))
    return batch


@router.get("/{batch_id}/health", response_model=BatchHealthOut)
async def get_batch_health(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> BatchHealthOut:
    """Return stalled / failure signals for one batch (§16.5)."""
    await _require_visible_batch(batch_id, current, session, locale)
    key = f"batch-health:u{current.id}:{batch_id}"

    async def _load() -> BatchHealthOut:
        threshold = int(
            await get_flag(session, "stalled_threshold_sec", default=300)
        )
        payload = await batch_health(
            batch_id, session, stalled_threshold_s=threshold
        )
        return BatchHealthOut.model_validate(payload)

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{batch_id}/eta", response_model=BatchEtaOut)
async def get_batch_eta(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> BatchEtaOut:
    """EMA-based ETA over the 10 most recent completed jobs (§16.5)."""
    batch = await _require_visible_batch(batch_id, current, session, locale)
    key = f"batch-eta:u{current.id}:{batch_id}"

    async def _load() -> BatchEtaOut:
        pending: int = 0
        if batch.n_total is not None:
            pending = max(
                0,
                batch.n_total
                - (batch.n_done or 0)
                - (batch.n_failed or 0),
            )

        rows = (
            await session.execute(
                select(Job.elapsed_s)
                .where(Job.batch_id == batch_id)
                .where(Job.is_deleted.is_(False))
                .where(func.lower(Job.status) == "done")
                .where(Job.elapsed_s.is_not(None))
                .order_by(Job.end_time.desc().nullslast())
                .limit(10)
            )
        ).all()
        elapsed = [r[0] for r in rows if r[0] is not None]

        eta = ema_eta(elapsed, pending) if pending > 0 else 0
        return BatchEtaOut(
            batch_id=batch_id,
            eta_seconds=eta,
            pending_count=pending,
            sampled_done_jobs=len(elapsed),
        )

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{batch_id}/jobs/eta-all")
async def get_batch_jobs_eta_all(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> dict[str, Any]:
    """Bulk per-job ETA for every job in a batch (10-second in-process cache).

    Returns ``{job_id: {eta_s, eta_iso, epochs_done, epochs_total,
    avg_epoch_time_s, elapsed_s}}`` for every job.  Use this in the
    BatchDetail Jobs tab instead of N individual job-eta calls.
    """
    await _require_visible_batch(batch_id, current, session, locale)
    # Per-user key mirrors the other batch endpoints; the payload itself is
    # visibility-agnostic but keying per user keeps the invalidation story
    # uniform with ``batch:``/``batch-jobs:`` and avoids the shared-entry
    # edge case if visibility ever grows a per-user filter.
    key = f"batch-jobs-eta-all:u{current.id}:{batch_id}"

    async def _load() -> dict[str, Any]:
        # Fetch all jobs for the batch
        jobs = (
            await session.execute(
                select(Job)
                .where(Job.batch_id == batch_id)
                .where(Job.is_deleted.is_(False))
            )
        ).scalars().all()

        # Fetch all job_epoch timestamps for the whole batch at once (1 query)
        epoch_rows = (
            await session.execute(
                select(Event.job_id, Event.timestamp)
                .where(Event.batch_id == batch_id)
                .where(Event.event_type == "job_epoch")
                .where(Event.job_id.is_not(None))
                .order_by(Event.timestamp.asc(), Event.id.asc())
            )
        ).all()

        # Group timestamps by job_id
        epoch_map: dict[str, list[str]] = {}
        for jid, ts in epoch_rows:
            if jid and ts:
                epoch_map.setdefault(jid, []).append(ts)

        result: dict[str, Any] = {}
        for job in jobs:
            # Pull train_epochs from metrics
            train_epochs: int | None = None
            if job.metrics:
                try:
                    m = json.loads(job.metrics)
                    if isinstance(m, dict):
                        for mk in ("train_epochs", "epochs", "n_epochs"):
                            v = m.get(mk)
                            if isinstance(v, int) and v > 0:
                                train_epochs = v
                                break
                except (json.JSONDecodeError, TypeError):
                    pass

            eta = compute_job_eta(
                job_id=job.id,
                job_start_iso=job.start_time,
                train_epochs_config=train_epochs,
                epoch_timestamps=epoch_map.get(job.id, []),
            )
            result[job.id] = {
                "eta_s": eta.eta_s,
                "eta_iso": eta.eta_iso,
                "epochs_done": eta.epochs_done,
                "epochs_total": eta.epochs_total,
                "avg_epoch_time_s": eta.avg_epoch_time_s,
                "elapsed_s": eta.elapsed_s,
            }
        return result

    return await _response_cache.get_or_compute(key, _load)


def _format_metric(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


@router.get("/{batch_id}/export.csv")
async def export_batch_csv(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> StreamingResponse:
    """Download the batch's job leaderboard as CSV.

    Columns: ``batch_id, model, dataset, status, epochs, elapsed_s``
    followed by every metric in :data:`_STANDARD_METRICS`. Streaming
    writer keeps memory flat on batches with hundreds of jobs.
    """
    await _require_visible_batch(batch_id, current, session, locale)

    jobs = (
        await session.execute(
            select(Job)
            .where(Job.batch_id == batch_id)
            .where(Job.is_deleted.is_(False))
            .order_by(Job.start_time.asc().nullslast(), Job.id.asc())
        )
    ).scalars().all()

    header = [
        "batch_id", "job_id", "model", "dataset", "status",
        "epochs", "elapsed_s", *_STANDARD_METRICS,
    ]
    data_rows: list[list[str]] = [header]
    for job in jobs:
        metrics: dict[str, Any] = {}
        if job.metrics:
            try:
                parsed = json.loads(job.metrics)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                metrics = parsed
        epochs = metrics.get("epochs") or metrics.get("train_epochs")
        data_rows.append([
            job.batch_id,
            job.id,
            job.model or "",
            job.dataset or "",
            job.status or "",
            str(epochs) if epochs is not None else "",
            str(job.elapsed_s) if job.elapsed_s is not None else "",
            *[_format_metric(metrics.get(name)) for name in _STANDARD_METRICS],
        ])

    async def _iter() -> AsyncIterator[bytes]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in data_rows:
            writer.writerow(row)
            chunk = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            if chunk:
                yield chunk.encode("utf-8")

    safe = batch_id.replace("/", "_").replace(" ", "_")
    return StreamingResponse(
        _iter(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.csv"'
        },
    )


# ---------------------------------------------------------------------------
# Dashboard data-gap routes: resources / log-lines / epochs/latest
# ---------------------------------------------------------------------------


class BatchResourcesOut(BaseModel):
    """``GET /api/batches/{id}/resources`` — latest host resource snapshots."""

    model_config = {"extra": "forbid"}

    host: str
    snapshots: list[dict]  # {ts, gpu_util, vram_used_mb, vram_total_mb, cpu_util, ram_used_mb, ram_total_mb, disk_free_gb, proc_cpu_pct, proc_ram_mb, proc_gpu_mem_mb}


class LogLineOut(BaseModel):
    """One log-line event row."""

    model_config = {"extra": "forbid"}

    ts: str
    job_id: str | None
    level: str
    line: str


class JobEpochLatestOut(BaseModel):
    """Latest epoch summary for one job."""

    model_config = {"extra": "forbid"}

    job_id: str
    epoch: int
    train_loss: float | None
    val_loss: float | None
    lr: float | None
    val_loss_trace: list[float]  # last 20 epochs, chronological


class BatchEpochsLatestOut(BaseModel):
    """``GET /api/batches/{id}/epochs/latest`` — per-job latest epoch."""

    model_config = {"extra": "forbid"}

    jobs: list[JobEpochLatestOut]


@router.get("/{batch_id}/resources", response_model=BatchResourcesOut)
async def get_batch_resources(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> BatchResourcesOut:
    """Return the 100 most-recent ResourceSnapshot rows for the batch host."""
    batch = await _require_visible_batch(batch_id, current, session, locale)
    # Key includes the ``limit=100`` window so if that ever becomes a
    # query param the cache stays correct.
    key = f"batch-resources:u{current.id}:{batch_id}:100"

    async def _load() -> BatchResourcesOut:
        host = batch.host or ""
        rows = (
            await session.execute(
                select(ResourceSnapshot)
                .where(ResourceSnapshot.host == host)
                .order_by(ResourceSnapshot.timestamp.desc())
                .limit(100)
            )
        ).scalars().all()

        snapshots = [
            {
                "ts": r.timestamp,
                "gpu_util": r.gpu_util_pct,
                "vram_used_mb": r.gpu_mem_mb,
                "vram_total_mb": r.gpu_mem_total_mb,
                "cpu_util": r.cpu_util_pct,
                "ram_used_mb": r.ram_mb,
                "ram_total_mb": r.ram_total_mb,
                "disk_free_gb": (
                    round(r.disk_free_mb / 1024, 2)
                    if r.disk_free_mb is not None
                    else None
                ),
                # per-process fields (migration 008); null for old rows
                "proc_cpu_pct": r.proc_cpu_pct,
                "proc_ram_mb": r.proc_ram_mb,
                "proc_gpu_mem_mb": r.proc_gpu_mem_mb,
            }
            for r in rows
        ]
        return BatchResourcesOut(host=host, snapshots=snapshots)

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{batch_id}/log-lines", response_model=list[LogLineOut])
async def get_batch_log_lines(
    batch_id: str,
    limit: int = Query(default=200, ge=1, le=2000),
    job_id: str | None = Query(default=None),
    bust: str | None = Query(default=None),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> list[LogLineOut]:
    """Return the last *limit* ``log_line`` events for the batch.

    Optionally filter by *job_id*.  Results are returned newest-first so
    the UI can page forward from the most-recent entry.

    Default limit was bumped 50→200 (cap 500→2000) so a healthy run with
    INFO-level logs surfaces a useful chunk of context on first page load.

    Cached for 10s under the shared :data:`default_cache`. BatchDetail's
    Logs tab polls every 10s, plus an SSE-driven refresh on every
    ``log_line`` event — the cache absorbs the bursty re-fetches that
    happen when the user opens the page or switches tabs.

    Pass ``?bust=<anything>`` to bypass the response cache; the UI's
    Refresh button uses this to force a fresh DB read while debugging.
    """
    await _require_visible_batch(batch_id, current, session, locale)
    key = f"batch-log-lines:u{current.id}:{batch_id}:{job_id or ''}:{limit}"
    if bust is not None:
        # Make the cache key globally unique so the cached entry is bypassed
        # AND the new fetch isn't reused by any concurrent caller.
        key = f"{key}:bust{bust}"

    async def _load() -> list[LogLineOut]:
        stmt = (
            select(Event)
            .where(Event.batch_id == batch_id)
            .where(Event.event_type == "log_line")
        )
        if job_id is not None:
            stmt = stmt.where(Event.job_id == job_id)
        stmt = stmt.order_by(Event.timestamp.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

        out: list[LogLineOut] = []
        for ev in rows:
            data: dict[str, Any] = {}
            if ev.data:
                try:
                    data = json.loads(ev.data)
                except (json.JSONDecodeError, TypeError):
                    data = {}
            out.append(
                LogLineOut(
                    ts=ev.timestamp,
                    job_id=ev.job_id,
                    level=data.get("level", "info"),
                    line=data.get("line", ""),
                )
            )
        return out

    return await _response_cache.get_or_compute(key, _load)


@router.get("/{batch_id}/epochs/latest", response_model=BatchEpochsLatestOut)
async def get_batch_epochs_latest(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> BatchEpochsLatestOut:
    """Return, for each job in the batch, its latest epoch metrics + a
    20-point val_loss trace (chronological, oldest first).
    """
    await _require_visible_batch(batch_id, current, session, locale)
    key = f"batch-epochs-latest:u{current.id}:{batch_id}"

    async def _load() -> BatchEpochsLatestOut:
        # Fetch all job_epoch events for this batch ordered chronologically.
        epoch_rows = (
            await session.execute(
                select(Event)
                .where(Event.batch_id == batch_id)
                .where(Event.event_type == "job_epoch")
                .where(Event.job_id.is_not(None))
                .order_by(Event.timestamp.asc())
            )
        ).scalars().all()

        # Group by job_id; accumulate val_loss trace and keep last row per
        # job. Structure: {job_id: {"latest": dict, "trace": [float]}}
        per_job: dict[str, dict[str, Any]] = {}
        for ev in epoch_rows:
            data: dict[str, Any] = {}
            if ev.data:
                try:
                    data = json.loads(ev.data)
                except (json.JSONDecodeError, TypeError):
                    data = {}
            jid = ev.job_id or ""
            if jid not in per_job:
                per_job[jid] = {"latest": {}, "trace": []}
            per_job[jid]["latest"] = data
            vl = data.get("val_loss")
            if vl is not None:
                per_job[jid]["trace"].append(float(vl))

        jobs_out: list[JobEpochLatestOut] = []
        for jid, state in per_job.items():
            d = state["latest"]
            trace = state["trace"]
            jobs_out.append(
                JobEpochLatestOut(
                    job_id=jid,
                    epoch=int(d.get("epoch", 0)),
                    train_loss=(
                        float(d["train_loss"]) if d.get("train_loss") is not None else None
                    ),
                    val_loss=(
                        float(d["val_loss"]) if d.get("val_loss") is not None else None
                    ),
                    lr=float(d["lr"]) if d.get("lr") is not None else None,
                    val_loss_trace=trace[-20:],
                )
            )
        return BatchEpochsLatestOut(jobs=jobs_out)

    return await _response_cache.get_or_compute(key, _load)


# ---------------------------------------------------------------------------
# Stop-signal endpoints
# ---------------------------------------------------------------------------


class StopRequestedOut(BaseModel):
    """Response body for ``GET /{batch_id}/stop-requested``."""

    model_config = {"extra": "forbid"}

    requested: bool
    requested_at: str | None = None
    requested_by: str | None = None


@router.post("/{batch_id}/stop", status_code=200)
async def stop_batch(
    batch_id: str,
    request: Request,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> dict:
    """Signal a stop request for a running batch.

    Sets ``Batch.status`` to ``'stopping'``, writes an audit log row, and
    emits a ``batch_stop_requested`` Event row so reporters can poll
    ``GET /{batch_id}/stop-requested`` and terminate the experiment.

    No process is killed — this is a cooperative signal only. Idempotent:
    calling it on an already-``'stopping'`` batch returns 200 without
    double-logging.

    Permission: owner or admin only.
    """
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.is_deleted:
        raise HTTPException(status_code=404, detail=tr(locale, "batch.not_found"))

    # Owner-or-admin guard — mirrors the pattern in shares.py
    if not current.is_admin and batch.owner_id != current.id:
        raise HTTPException(
            status_code=403,
            detail=tr(locale, "share.batch.owner_only"),
        )

    # Delegate to the Executor service so rerun/stop share one
    # idempotency / event-emission codepath. The audit log + cache bust
    # remain at the route layer (existing pattern preserved so we don't
    # regress access-log redaction or per-user cache invalidation).
    executor = get_executor()
    try:
        result = await executor.request_stop(
            session,
            batch_id=batch_id,
            user_id=current.id,
            username=current.username,
        )
    except ExecutorBatchNotFound:
        raise HTTPException(
            status_code=404, detail=tr(locale, "batch.not_found")
        )
    except InvalidSourceState as exc:
        raise HTTPException(status_code=409, detail=exc.detail)

    if result.noop:
        return {"status": result.status, "batch_id": batch_id}

    await session.commit()

    # Bust per-user batch caches so the flipped status surfaces on the
    # next read instead of waiting out the 10s TTL. Imported here to
    # avoid a circular import with the shares router at module load.
    from backend.api.shares import _bust_batch_cache_for_user

    _bust_batch_cache_for_user(batch_id, current.id)

    # Audit log — fire-and-forget; a hiccup here must not roll back the stop
    get_audit_service().log_background(
        action="batch_stop_requested",
        user_id=current.id,
        target_type="batch",
        target_id=batch_id,
        metadata={"requested_by": current.username},
        ip=(request.client.host if request.client else None),
    )

    return {"status": "stopping", "batch_id": batch_id}


@router.get("/{batch_id}/stop-requested", response_model=StopRequestedOut)
async def get_stop_requested(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> StopRequestedOut:
    """Poll whether a stop has been requested for a batch.

    Intended for the reporter client (authenticates via a personal API
    token). Rate-limited to 60/min via the shared default bucket so an
    aggressive polling loop does not exhaust the ingest budget.

    Returns ``{requested: false}`` when no stop is pending.
    """
    # Rate-limit: 1 token per call, keyed on user identity
    bucket = get_default_bucket()
    key = f"stop_requested:{current.id}"
    allowed, retry_after = await bucket.try_consume(key)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Retry after {retry_after:.0f}s.",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    batch = await session.get(Batch, batch_id)
    if batch is None or batch.is_deleted:
        raise HTTPException(status_code=404, detail=tr(locale, "batch.not_found"))

    # Any authenticated user who can view the batch may poll this endpoint
    resolver = VisibilityResolver()
    if not await resolver.can_view_batch(current, batch_id, session):
        raise HTTPException(status_code=404, detail=tr(locale, "batch.not_found"))

    if batch.status != "stopping":
        return StopRequestedOut(requested=False)

    # Find the most recent batch_stop_requested event for metadata
    row = (
        await session.execute(
            select(Event)
            .where(Event.batch_id == batch_id)
            .where(Event.event_type == "batch_stop_requested")
            .order_by(Event.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    requested_at: str | None = None
    requested_by: str | None = None
    if row is not None and row.data:
        try:
            d = json.loads(row.data)
            requested_at = d.get("requested_at")
            requested_by = d.get("requested_by")
        except (json.JSONDecodeError, TypeError):
            pass

    return StopRequestedOut(
        requested=True,
        requested_at=requested_at,
        requested_by=requested_by,
    )


# ---------------------------------------------------------------------------
# Soft delete (migration 021) — owner or admin
# ---------------------------------------------------------------------------


class BulkDeleteBatchesIn(BaseModel):
    """Request body for ``POST /batches/bulk-delete``.

    Capped at 500 ids per request (v0.1.3 hardening) so a runaway client
    can't tie up the worker for minutes on a single connection — pydantic
    rejects oversize payloads with 422 before the handler runs.
    """

    model_config = {"extra": "forbid"}

    batch_ids: list[str] = Field(max_length=500)


class BulkDeleteSkip(BaseModel):
    """One row of the ``skipped`` list returned by bulk-delete endpoints."""

    model_config = {"extra": "forbid"}

    id: str
    reason: str


class BulkDeleteOut(BaseModel):
    """Common response shape for bulk-delete endpoints."""

    model_config = {"extra": "forbid"}

    deleted: list[str]
    skipped: list[BulkDeleteSkip]


@router.post("/bulk-delete", response_model=BulkDeleteOut, status_code=200)
async def bulk_delete_batches(
    payload: BulkDeleteBatchesIn,
    request: Request,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> BulkDeleteOut:
    """Soft-delete many batches in one round-trip.

    Each id is checked individually: missing / already-deleted / not-owner
    rows go into ``skipped`` with a structured reason; everything else is
    flagged ``is_deleted=True`` and audit-logged. Empty input is a 400 so
    the caller's UI surfaces it clearly.
    """
    if not payload.batch_ids:
        raise HTTPException(status_code=400, detail="batch_ids must be non-empty")

    deleted: list[str] = []
    skipped: list[BulkDeleteSkip] = []
    audit = get_audit_service()
    ip = request.client.host if request.client else None

    for batch_id in payload.batch_ids:
        batch = await session.get(Batch, batch_id)
        if batch is None:
            skipped.append(BulkDeleteSkip(id=batch_id, reason="not_found"))
            continue
        if batch.is_deleted:
            skipped.append(BulkDeleteSkip(id=batch_id, reason="already_deleted"))
            continue
        if not current.is_admin and batch.owner_id != current.id:
            skipped.append(BulkDeleteSkip(id=batch_id, reason="not_owner"))
            continue
        # Safety guard (v0.1.3): never delete an active batch out from
        # under a running reporter. We route it into ``skipped`` rather
        # than 409-ing the whole call so the UI can still progress on
        # the dormant rows in the same payload.
        if batch.status in {"running", "pending", "stopping"}:
            skipped.append(
                BulkDeleteSkip(id=batch_id, reason=batch.status)
            )
            continue
        batch.is_deleted = True
        deleted.append(batch_id)
        audit.log_background(
            action="batch_deleted",
            user_id=current.id,
            target_type="batch",
            target_id=batch_id,
            metadata={"batch_id": batch_id, "via": "bulk"},
            ip=ip,
        )

    if deleted:
        await session.commit()
        # Cache-bust each deleted batch for the caller.
        from backend.api.shares import _bust_batch_cache_for_user

        for bid in deleted:
            _bust_batch_cache_for_user(bid, current.id)
        # Also bust list / compact caches for the caller so the next
        # render reflects the bulk deletion immediately.
        _response_cache.invalidate_prefix(f"batches-list:u{current.id}:")
        _response_cache.invalidate_prefix(f"batches-compact:u{current.id}:")

    return BulkDeleteOut(deleted=deleted, skipped=skipped)


@router.delete("/{batch_id}", status_code=200)
async def delete_batch(
    batch_id: str,
    request: Request,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> dict:
    """Soft-delete a batch.

    Sets ``Batch.is_deleted=True``; every existing list / detail
    surface already filters on this column so the batch immediately
    drops out of the UI. The row stays for audit + retention purposes.

    Idempotent: deleting an already-deleted batch returns 404 (the
    caller can no longer see it). Owner or admin only — same rule
    applied by stop / rerun.
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

    # Safety guard (v0.1.3): refuse deletion while the batch is still
    # active. A user racing the kill signal with the delete button
    # would otherwise leave behind an orphaned process whose status
    # row no longer exists. They must POST /stop first and wait for
    # the reporter to flip the status to done/failed/stopped.
    if batch.status in {"running", "pending", "stopping"}:
        raise HTTPException(
            status_code=409,
            detail=tr(locale, "batch.delete_blocked_running"),
        )

    batch.is_deleted = True
    await session.commit()

    # Bust every per-user cache entry for this batch + the caller's
    # batch-list / compact caches so the next read reflects the
    # deletion immediately.
    from backend.api.shares import _bust_batch_cache_for_user

    _bust_batch_cache_for_user(batch_id, current.id)

    get_audit_service().log_background(
        action="batch_deleted",
        user_id=current.id,
        target_type="batch",
        target_id=batch_id,
        metadata={"batch_id": batch_id},
        ip=(request.client.host if request.client else None),
    )

    return {"status": "deleted", "batch_id": batch_id}


# ---------------------------------------------------------------------------
# Rerun-with-overrides endpoints (migration 012 added Batch.source_batch_id)
# ---------------------------------------------------------------------------


# 64 KB cap on the JSON-serialised ``overrides`` payload (v0.1.3
# hardening). Big enough for any legitimate Hydra override block —
# blocks the obvious DoS where a client posts a megabytes-of-junk
# payload to amplify a one-line API call.
_RERUN_OVERRIDES_MAX_BYTES = 64 * 1024


class RerunIn(BaseModel):
    """Request body for POST /batches/{id}/rerun."""

    model_config = {"extra": "forbid"}

    overrides: dict[str, Any] = {}
    name: str | None = None

    @field_validator("overrides")
    @classmethod
    def _check_overrides_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Reject overrides that serialise to more than 64 KB of JSON."""
        try:
            serialised = json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"overrides must be JSON-serialisable: {exc}"
            ) from exc
        if len(serialised.encode("utf-8")) > _RERUN_OVERRIDES_MAX_BYTES:
            raise ValueError(
                "overrides payload exceeds 64 KB cap"
            )
        return v


class RerunOut(BaseModel):
    """Response body for POST /batches/{id}/rerun — the new batch id."""

    model_config = {"extra": "forbid"}

    batch_id: str
    source_batch_id: str
    name: str | None = None
    status: str = "requested"


class RerunInfoOut(BaseModel):
    """Response body for GET /batches/{id}/rerun-info (reporter poll)."""

    model_config = {"extra": "forbid"}

    source_batch_id: str | None = None
    overrides_json: str | None = None
    requested_at: str | None = None
    requested_by: str | None = None


@router.post("/{batch_id}/rerun", response_model=RerunOut, status_code=201)
async def rerun_batch(
    batch_id: str,
    payload: RerunIn,
    request: Request,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> RerunOut:
    """Mint a new Batch row cloned from *batch_id* with hyperparameter overrides.

    The monitor only records the intent — no training process is spawned.
    A cooperating reporter polls :endpoint:`/batches/{id}/rerun-info`
    to pick up the request and launch the actual `main.py`.

    Owner-or-admin only.
    """
    source = await session.get(Batch, batch_id)
    if source is None or source.is_deleted:
        raise HTTPException(status_code=404, detail=tr(locale, "batch.not_found"))
    if not current.is_admin and source.owner_id != current.id:
        raise HTTPException(
            status_code=403,
            detail=tr(locale, "share.batch.owner_only"),
        )

    # Executor handles batch row creation, rerun_requested event, and
    # AgentCommand enqueue (when an agent is registered for the source
    # host). When no agent exists the new batch still lands in
    # ``status='requested'`` and the operator can start an agent or
    # launch the command manually — this is the documented escape hatch
    # in the architect's design doc Section 2.
    executor = get_executor()
    try:
        result = await executor.request_rerun(
            session,
            source_batch_id=batch_id,
            user_id=current.id,
            username=current.username,
            overrides=payload.overrides,
            custom_name=payload.name,
        )
    except ExecutorBatchNotFound:
        raise HTTPException(
            status_code=404, detail=tr(locale, "batch.not_found")
        )
    except InvalidSourceState as exc:
        raise HTTPException(status_code=409, detail=exc.detail)

    await session.commit()

    get_audit_service().log_background(
        action="batch_rerun_requested",
        user_id=current.id,
        target_type="batch",
        target_id=result.new_batch_id,
        metadata={
            "source_batch_id": batch_id,
            "overrides": payload.overrides,
            "deduped": result.deduped,
        },
        ip=(request.client.host if request.client else None),
    )

    return RerunOut(
        batch_id=result.new_batch_id,
        source_batch_id=batch_id,
        name=result.new_batch_name,
        status="requested",
    )


@router.get("/{batch_id}/rerun-info", response_model=RerunInfoOut)
async def get_rerun_info(
    batch_id: str,
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> RerunInfoOut:
    """Return rerun metadata for a batch (used by reporter-side launcher).

    Rate-limited via the shared default bucket.
    """
    bucket = get_default_bucket()
    allowed, retry_after = await bucket.try_consume(
        f"rerun-info:{current.id}"
    )
    if not allowed:
        retry_seconds = max(1, int(retry_after + 0.999))
        raise HTTPException(
            status_code=429,
            detail=tr(locale, "rate.too_fast"),
            headers={"Retry-After": str(retry_seconds)},
        )

    batch = await session.get(Batch, batch_id)
    if batch is None or batch.is_deleted:
        raise HTTPException(status_code=404, detail=tr(locale, "batch.not_found"))

    if batch.source_batch_id is None:
        return RerunInfoOut()

    row = (
        await session.execute(
            select(Event)
            .where(Event.batch_id == batch_id, Event.event_type == "rerun_requested")
            .order_by(Event.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    overrides_json: str | None = None
    requested_at: str | None = None
    requested_by: str | None = None
    if row is not None and row.data:
        try:
            d = json.loads(row.data)
            overrides_json = json.dumps(d.get("overrides") or {}, ensure_ascii=False)
            requested_at = d.get("requested_at")
            requested_by = d.get("requested_by")
        except (json.JSONDecodeError, TypeError):
            pass

    return RerunInfoOut(
        source_batch_id=batch.source_batch_id,
        overrides_json=overrides_json,
        requested_at=requested_at,
        requested_by=requested_by,
    )
