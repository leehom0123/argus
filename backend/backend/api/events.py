"""POST /api/events — ingest endpoint.

This is the single write path into the database. Responsibilities:

1. Authenticate via a reporter-scope API token (``em_live_`` prefix).
2. Apply per-token rate limiting.
3. Validate envelope with :class:`EventIn` (422 on bad shape).
4. Enforce ``schema_version == "1.1"``; return 415 otherwise (v1.0 was
   supported during the Phase-3 migration window and is now rejected).
5. Enforce idempotency by client-supplied ``event_id``: on hit, short-
   circuit with 200 + ``deduplicated=True``.
6. Validate the ``data`` payload against the per-type model.
7. Persist the raw event row.
8. Apply side effects to ``batch`` / ``job`` / ``resource_snapshot``
   rows. First touch of a new batch stamps ``owner_id`` from the token.
9. Commit.
10. Dispatch matching notification rules via ``asyncio.create_task``
    so the HTTP response returns quickly.

``POST /api/events/batch`` wraps the same per-event logic in a loop
with a per-item outcome report (see requirements §6.1).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_session
from backend.deps import current_token_user_id, enforce_ingest_rate_limit
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, Event, Job, ResourceSnapshot, User
from backend.notifications import evaluate
from backend.schemas import (
    PAYLOAD_MODELS,
    BatchEventResult,
    BatchEventsIn,
    BatchEventsOut,
    EventAccepted,
    EventIn,
)
from backend.services.sse_hub import publish_to_sse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["events"])


# Accepted wire formats. v1.0 was deprecated during Phase 3 and is now
# rejected outright (415) — the only supported client contract is v1.1,
# which requires a client-generated ``event_id`` for safe retry
# deduplication. See docs/requirements.md §6.5.
_SUPPORTED_SCHEMA_VERSIONS = {"1.1"}


# Module-level strong-reference container for in-flight background tasks
# (Round-2 M5 carryover). Without this Python's GC can reap the task
# mid-flight; the done-callback also makes sure any exception raised
# inside the coroutine shows up in logs instead of being silently lost.
_PENDING_TASKS: set[asyncio.Task] = set()


def _log_task_exceptions(task: asyncio.Task) -> None:
    """Log any exception that escaped a background task, then drop the ref."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        exc = None
    if exc is not None:
        log.warning("background task %r failed: %r", task.get_name(), exc)
    _PENDING_TASKS.discard(task)


def _spawn_dispatch(request: Request, event_payload: dict[str, Any]) -> None:
    """Schedule ``_dispatch_notifications`` with proper ref retention."""
    try:
        task = asyncio.create_task(
            _dispatch_notifications(request, event_payload),
            name=f"notify-{event_payload.get('event_type', '?')}",
        )
    except RuntimeError:
        # No running loop (e.g. sync test context) — skip scheduling.
        log.debug("no running event loop; skipping notification dispatch")
        return
    _PENDING_TASKS.add(task)
    task.add_done_callback(_log_task_exceptions)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> str | None:
    """JSON-encode a dict/list, tolerating None."""
    if value is None:
        return None
    return json.dumps(value, default=str, sort_keys=True)


async def _get_or_stub_batch(
    session: AsyncSession,
    event: EventIn,
    owner_id: int | None,
) -> Batch:
    """Return the batch row, creating a stub if unknown.

    ``owner_id`` is stamped only on the *initial* stub — subsequent
    events on the same batch never overwrite it. This matches the
    intent of "first writer wins" ownership: we don't want a lurking
    ``resource_snapshot`` from another token to silently reassign the
    batch mid-flight.
    """
    row = await session.get(Batch, event.batch_id)
    if row is not None:
        return row
    src = event.source
    row = Batch(
        id=event.batch_id,
        project=src.project,
        user=src.user,
        host=src.host,
        command=src.command,
        status="running",
        start_time=event.timestamp,
        owner_id=owner_id,
    )
    session.add(row)
    await session.flush()
    return row


async def _get_or_stub_job(
    session: AsyncSession, batch_id: str, job_id: str, timestamp: str
) -> Job:
    """Return the (batch, job) row, creating a stub if unknown."""
    row = await session.get(Job, (job_id, batch_id))
    if row is not None:
        return row
    row = Job(
        id=job_id,
        batch_id=batch_id,
        status="running",
        start_time=timestamp,
    )
    session.add(row)
    await session.flush()
    return row


async def _handle_batch_start(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    batch = await session.get(Batch, event.batch_id)
    is_new = batch is None
    if is_new:
        batch = Batch(
            id=event.batch_id,
            project=event.source.project,
            owner_id=owner_id,
        )
        session.add(batch)
    # Always-safe metadata refresh — these fields can carry late-arriving
    # info (e.g. command line shifted on resume).
    batch.project = event.source.project
    batch.user = event.source.user or batch.user
    batch.host = event.source.host or batch.host
    batch.command = data.get("command") or event.source.command or batch.command
    batch.experiment_type = data.get("experiment_type") or batch.experiment_type
    batch.n_total = data.get("n_total_jobs", batch.n_total)
    if is_new:
        # Fresh batch: stamp baseline timing + status.
        batch.status = "running"
        batch.start_time = event.timestamp
        batch.end_time = None
    else:
        # Idempotent re-init for resume-after-crash. Crash-recovery rule
        # (batch-resume v0.2.1):
        #   * Preserve the *original* start_time so historical timing
        #     stays intact across a resumed launcher invocation.
        #   * Only flip status back to "running" when the previous run
        #     left it in a non-terminal-success state (running/failed/
        #     unset). A successful "done" batch is left as-is so an
        #     accidental re-run with the same id doesn't undo finality
        #     — callers who really want to rerun should pass a new id.
        #   * Clear end_time only when we're transitioning back to
        #     running for the same reason.
        prev_status = (batch.status or "").lower()
        if prev_status != "done":
            batch.status = "running"
            batch.end_time = None
    # Never overwrite an already-set owner on subsequent batch_start
    # replays — owner is immutable once known.
    if batch.owner_id is None and owner_id is not None:
        batch.owner_id = owner_id


async def _dispatch_email_safe(
    session: AsyncSession,
    *,
    event_type: str,
    batch: Batch | None,
    job: Job | None = None,
) -> None:
    """Best-effort wrapper around the email dispatcher.

    Failures are logged at debug level and swallowed so a flaky SMTP
    transport never blocks the HTTP ingest path.
    """
    try:
        from backend.services.notifications_dispatcher import (
            dispatch_email_for_event,
        )
        await dispatch_email_for_event(
            session, event_type=event_type, batch=batch, job=job
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("email dispatch (%s) failed: %r", event_type, exc)


async def _handle_batch_done(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    batch = await _get_or_stub_batch(session, event, owner_id)
    batch.status = "done"
    batch.end_time = event.timestamp
    if data.get("n_done") is not None:
        batch.n_done = int(data["n_done"])
    if data.get("n_failed") is not None:
        batch.n_failed = int(data["n_failed"])
    await _dispatch_email_safe(
        session, event_type="batch_done", batch=batch
    )


async def _handle_batch_failed(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    batch = await _get_or_stub_batch(session, event, owner_id)
    batch.status = "failed"
    batch.end_time = event.timestamp
    if data.get("n_done") is not None:
        batch.n_done = int(data["n_done"])
    if data.get("n_failed") is not None:
        batch.n_failed = int(data["n_failed"])
    await _dispatch_email_safe(
        session, event_type="batch_failed", batch=batch
    )


async def _handle_job_start(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    if event.job_id is None:
        raise HTTPException(status_code=400, detail=tr(locale, "job.start.missing_job_id"))
    await _get_or_stub_batch(session, event, owner_id)
    job = await _get_or_stub_job(
        session, event.batch_id, event.job_id, event.timestamp
    )
    job.model = data.get("model") or job.model
    job.dataset = data.get("dataset") or job.dataset
    job.status = "running"
    job.start_time = event.timestamp
    extra = {
        k: v
        for k, v in data.items()
        if k not in {"model", "dataset"}
    }
    if extra:
        job.extra = _dumps(extra)


async def _handle_job_epoch(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    """Auto-create job stub; epoch data lives only in the event table."""
    if event.job_id is None:
        raise HTTPException(status_code=400, detail=tr(locale, "job.epoch.missing_job_id"))
    await _get_or_stub_batch(session, event, owner_id)
    await _get_or_stub_job(
        session, event.batch_id, event.job_id, event.timestamp
    )


async def _handle_job_done(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    if event.job_id is None:
        raise HTTPException(status_code=400, detail=tr(locale, "job.done.missing_job_id"))
    await _get_or_stub_batch(session, event, owner_id)
    job = await _get_or_stub_job(
        session, event.batch_id, event.job_id, event.timestamp
    )
    # last-write-wins idempotent update
    job.status = (data.get("status") or "done").lower()
    job.end_time = event.timestamp
    if data.get("elapsed_s") is not None:
        job.elapsed_s = int(float(data["elapsed_s"]))
    if data.get("metrics") is not None:
        job.metrics = _dumps(data["metrics"])

    # Recompute batch counters from authoritative job rows.
    await _recompute_batch_counters(session, event.batch_id)


async def _handle_job_failed(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    if event.job_id is None:
        raise HTTPException(
            status_code=400, detail=tr(locale, "job.failed.missing_job_id")
        )
    await _get_or_stub_batch(session, event, owner_id)
    job = await _get_or_stub_job(
        session, event.batch_id, event.job_id, event.timestamp
    )
    job.status = "failed"
    job.end_time = event.timestamp
    if data.get("elapsed_s") is not None:
        job.elapsed_s = int(float(data["elapsed_s"]))
    extra = {k: v for k, v in data.items() if k != "elapsed_s"}
    if extra:
        job.extra = _dumps(extra)

    await _recompute_batch_counters(session, event.batch_id)

    # Resolve the parent batch so the email dispatcher can address the
    # owner + share grantees without re-fetching it inside the helper.
    batch = (
        await session.execute(
            select(Batch).where(Batch.id == event.batch_id)
        )
    ).scalar_one_or_none()
    await _dispatch_email_safe(
        session, event_type="job_failed", batch=batch, job=job
    )


async def _handle_resource_snapshot(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    host = event.source.host or "unknown"
    known_fields = {
        "gpu_util_pct",
        "gpu_mem_mb",
        "gpu_mem_total_mb",
        "gpu_temp_c",
        "cpu_util_pct",
        "ram_mb",
        "ram_total_mb",
        "disk_free_mb",
        "disk_total_mb",
        # per-process fields (migration 008)
        "proc_cpu_pct",
        "proc_ram_mb",
        "proc_gpu_mem_mb",
    }
    snap = ResourceSnapshot(
        host=host,
        timestamp=event.timestamp,
        gpu_util_pct=data.get("gpu_util_pct"),
        gpu_mem_mb=data.get("gpu_mem_mb"),
        gpu_mem_total_mb=data.get("gpu_mem_total_mb"),
        gpu_temp_c=data.get("gpu_temp_c"),
        cpu_util_pct=data.get("cpu_util_pct"),
        ram_mb=data.get("ram_mb"),
        ram_total_mb=data.get("ram_total_mb"),
        disk_free_mb=data.get("disk_free_mb"),
        disk_total_mb=data.get("disk_total_mb"),
        # per-process fields
        proc_cpu_pct=data.get("proc_cpu_pct"),
        proc_ram_mb=data.get("proc_ram_mb"),
        proc_gpu_mem_mb=data.get("proc_gpu_mem_mb"),
        # batch_id comes from the event envelope, not from data
        batch_id=event.batch_id,
        extra=_dumps({k: v for k, v in data.items() if k not in known_fields})
        or None,
    )
    session.add(snap)


async def _handle_log_line(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    """log_line has no side effects beyond the raw event row."""
    # Ensure batch row exists so list views stay consistent.
    await _get_or_stub_batch(session, event, owner_id)


async def _handle_env_snapshot(
    session: AsyncSession,
    event: EventIn,
    data: dict[str, Any],
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> None:
    """Persist one-time reproducibility snapshot on the Batch row.

    Migration 014 added ``Batch.env_snapshot_json``. Reporter emits this
    once on ``on_train_begin`` with git SHA, pip freeze, Hydra config,
    etc. We JSON-encode the data blob and stash it — UI reads via
    ``/api/batches/{id}``.
    """
    batch = await _get_or_stub_batch(session, event, owner_id)
    if batch is None:
        return
    # First-write-wins: subsequent env_snapshot events for the same
    # batch (e.g. a worker-level resend or a second job starting) must
    # not clobber the initial reproducibility record. See requirements
    # for migration 014 — the snapshot describes the *launcher's*
    # environment at ``on_train_begin`` and should be immutable.
    if batch.env_snapshot_json is not None:
        return
    try:
        batch.env_snapshot_json = _dumps(data)
    except Exception:  # noqa: BLE001 — never let encoding break ingest
        pass


async def _recompute_batch_counters(
    session: AsyncSession, batch_id: str
) -> None:
    """Rebuild n_done / n_failed from actual job rows for this batch."""
    result = await session.execute(
        select(Job.status).where(Job.batch_id == batch_id)
    )
    statuses = [r[0] for r in result.all()]
    n_done = sum(1 for s in statuses if s and s.lower() == "done")
    n_failed = sum(1 for s in statuses if s and s.lower() == "failed")
    batch = await session.get(Batch, batch_id)
    if batch is not None:
        batch.n_done = n_done
        batch.n_failed = n_failed


HANDLERS = {
    "batch_start": _handle_batch_start,
    "batch_done": _handle_batch_done,
    "batch_failed": _handle_batch_failed,
    "job_start": _handle_job_start,
    "job_epoch": _handle_job_epoch,
    "job_done": _handle_job_done,
    "job_failed": _handle_job_failed,
    "resource_snapshot": _handle_resource_snapshot,
    "log_line": _handle_log_line,
    "env_snapshot": _handle_env_snapshot,
}


# ---------------------------------------------------------------------------
# Notification dispatch
# ---------------------------------------------------------------------------


async def _dispatch_notifications(
    request: Request, event_payload: dict[str, Any]
) -> None:
    """Fire notifications for any rules matching ``event_payload``.

    Runs in a background task so ingest latency isn't coupled to webhook
    round-trips. Errors are swallowed after logging.
    """
    app = request.app
    rules = getattr(app.state, "notification_rules", [])
    if not rules:
        return
    channels: dict[str, Any] = getattr(app.state, "notification_channels", {})
    if not channels:
        return
    targets = evaluate(event_payload, rules)
    if not targets:
        return
    title = f"{event_payload.get('event_type')} / {event_payload.get('batch_id')}"
    body = json.dumps(event_payload.get("data") or {}, default=str)[:2000]
    level = (
        "error"
        if event_payload.get("event_type") in {"job_failed", "batch_failed"}
        else "info"
    )
    for name in targets:
        channel = channels.get(name)
        if channel is None:
            log.debug("no channel registered for %r", name)
            continue
        try:
            await channel.send(title, body, level)
        except Exception as exc:  # noqa: BLE001
            log.warning("notification channel %s failed: %s", name, exc)


# ---------------------------------------------------------------------------
# Core ingest: one event
# ---------------------------------------------------------------------------


def _check_schema_version(schema_version: str) -> None:
    """Raise 415 for any schema_version other than the one we support.

    Phase-3 post-review tightening (M2): v1.0 was the soft-compatible
    legacy wire format during the migration window; once all reporter
    clients shipped with v1.1 we flipped to strict enforcement so the
    contract is unambiguous. The 415 body still includes the
    ``supported`` list to keep the error machine-readable.
    """
    if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "message": "Unsupported schema_version",
                "received": schema_version,
                "supported": sorted(_SUPPORTED_SCHEMA_VERSIONS),
            },
        )


def _check_event_id_presence(event: EventIn, locale: SupportedLocale = "en-US") -> None:
    """v1.1 requires ``event_id``; v1.0 tolerates its absence."""
    if event.schema_version == "1.1" and not event.event_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=tr(locale, "event.schema.event_id_required"),
        )


async def _ingest_one(
    session: AsyncSession,
    event: EventIn,
    owner_id: int | None,
    locale: SupportedLocale = "en-US",
) -> tuple[Event, bool]:
    """Do the meaty per-event work inside an open session.

    Returns ``(row, deduplicated)``:
      * ``row`` is the persisted (or already-existing) Event row
      * ``deduplicated`` is True when we short-circuited on the
        ``event_id`` unique index.

    Caller is responsible for ``await session.commit()`` — doing the
    commit here would break the batch endpoint's single-transaction
    semantics on partial failure.
    """
    # 1. Idempotency check.
    if event.event_id:
        existing = await session.scalar(
            select(Event).where(Event.event_id == event.event_id)
        )
        if existing is not None:
            return existing, True

    # 2. Validate the per-type ``data`` payload.
    payload_model = PAYLOAD_MODELS.get(event.event_type)
    if payload_model is not None:
        try:
            payload_model.model_validate(event.data)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=exc.errors(),
            ) from exc

    # 3. Persist raw event first so even if side effects explode we keep audit.
    ev_row = Event(
        batch_id=event.batch_id,
        job_id=event.job_id,
        event_type=event.event_type,
        timestamp=event.timestamp,
        schema_version=event.schema_version,
        data=_dumps(event.data),
        event_id=event.event_id,
    )
    session.add(ev_row)
    await session.flush()

    # 4. Side effects on the batch / job / resource tables.
    handler = HANDLERS.get(event.event_type)
    if handler is not None:
        await handler(session, event, event.data, owner_id, locale)

    return ev_row, False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/events", response_model=EventAccepted)
async def ingest_event(
    event: EventIn,
    request: Request,
    user: User = Depends(enforce_ingest_rate_limit),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> EventAccepted:
    """Ingest one event, apply side effects, schedule notifications."""
    _check_schema_version(event.schema_version)
    _check_event_id_presence(event, locale)

    # Resolve owner_id from the API token's ``user_id`` column directly
    # rather than ``user.id`` (the eager-loaded relationship). Belt-and-
    # braces: ``current_token_user_id`` reads ``request.state`` so it
    # cannot raise greenlet/lazy-load surprises (#127).
    owner_id = current_token_user_id(request) or user.id
    ev_row, deduped = await _ingest_one(session, event, owner_id=owner_id, locale=locale)
    await session.commit()
    if not deduped:
        await session.refresh(ev_row)

    # Notification dispatch + SSE broadcast only on fresh inserts —
    # re-sends shouldn't re-trigger downstream consumers.
    if not deduped:
        event_payload = event.model_dump()
        _spawn_dispatch(request, event_payload)
        # Fire-and-forget fan-out to any live SSE subscribers.
        publish_to_sse(event_payload)

    return EventAccepted(
        accepted=True, event_id=ev_row.id, deduplicated=deduped
    )


@router.post("/events/batch", response_model=BatchEventsOut)
async def ingest_events_batch(
    payload: BatchEventsIn,
    request: Request,
    user: User = Depends(enforce_ingest_rate_limit),
    session: AsyncSession = Depends(get_session),
    locale: SupportedLocale = Depends(get_locale),
) -> BatchEventsOut:
    """Ingest up to 500 events in a single request.

    Per requirements §6.1 we return a per-event outcome report so
    clients can tell which specific events the server rejected. A
    per-event exception does NOT roll back the whole request — we
    commit successful events and surface failures as ``rejected``.
    """
    accepted = 0
    rejected = 0
    results: list[BatchEventResult] = []
    fired_notifications: list[dict[str, Any]] = []

    # Resolve owner_id once for the whole batch — every event in this
    # request shares the same auth context. See #127 for the column-vs-
    # relationship rationale.
    owner_id = current_token_user_id(request) or user.id

    for raw in payload.events:
        event_id = raw.event_id
        try:
            _check_schema_version(raw.schema_version)
            _check_event_id_presence(raw, locale)
            ev_row, deduped = await _ingest_one(session, raw, owner_id=owner_id, locale=locale)
            # Commit per event so a subsequent failure doesn't wipe
            # earlier progress. SQLite handles this fine; for a future
            # Postgres port consider a single transaction with
            # SAVEPOINT per item.
            await session.commit()
            if not deduped:
                await session.refresh(ev_row)
                event_payload = raw.model_dump()
                fired_notifications.append(event_payload)
                # SSE publish per event after commit — matches the
                # singleton endpoint's semantics so subscribers see
                # the same stream regardless of how the event arrived.
                publish_to_sse(event_payload)
            accepted += 1
            results.append(
                BatchEventResult(
                    event_id=event_id,
                    status="deduplicated" if deduped else "accepted",
                    db_id=ev_row.id,
                )
            )
        except HTTPException as exc:
            # Roll back any partial state for this specific event.
            await session.rollback()
            rejected += 1
            results.append(
                BatchEventResult(
                    event_id=event_id,
                    status="rejected",
                    error=str(exc.detail),
                )
            )
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            rejected += 1
            results.append(
                BatchEventResult(
                    event_id=event_id,
                    status="rejected",
                    error=f"internal: {exc!r}",
                )
            )
            log.exception("unexpected error on batch item: %s", exc)

    # Fire notifications for accepted non-dedup events after commit.
    for ev_payload in fired_notifications:
        _spawn_dispatch(request, ev_payload)

    return BatchEventsOut(
        accepted=accepted, rejected=rejected, results=results
    )
