"""In-app watchdog: rule engine + asyncio scan loop.

This module is **separate** from ``notifications/rules.py`` (the YAML-driven
Feishu outbound rules). The two coexist without touching each other:

* ``rules.py`` — parse YAML, fire to Feishu on raw event ingestion.
* ``watchdog.py`` — Python-coded rules that run every 60 s against DB state
  and insert rows into the ``notification`` table for the in-app bell.

Multi-worker note: if the deployment ever scales beyond a single uvicorn
worker process, both the watchdog loop and the retention sweeper will run in
*each* worker, causing duplicate notifications within the debounce window.
The deduplication query (``_already_fired``) provides best-effort protection
via a DB-level check, but a brief race between two concurrent iterations could
still produce a double-insert before either SELECT sees the other's row.
For now this is acceptable — single-worker deployments are the primary target.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Literal

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.db import SessionLocal
from backend.models import Batch, Event, Job, ResourceSnapshot, User

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain types used by predicates
# ---------------------------------------------------------------------------

Severity = Literal["info", "warn", "error"]


@dataclass
class WatchdogRule:
    """A single watchdog rule with its predicate and metadata.

    The predicate receives:
      * the :class:`Batch` row currently being inspected,
      * the list of :class:`Job` rows for that batch,
      * the last 50 :class:`Event` rows for that batch (newest first).

    It returns ``True`` when the alert condition is met.
    """

    id: str
    name: str
    predicate: Callable[[Batch, list[Job], list[Event]], bool]
    severity: Severity
    debounce_minutes: int = 30
    # Human-readable descriptions used for the notification body.
    title_tpl: str = ""
    body_tpl: str = ""


# ---------------------------------------------------------------------------
# Built-in rules (4 total — no more, no less)
# ---------------------------------------------------------------------------


def _rule_val_loss_diverging(
    batch: Batch, jobs: list[Job], events: list[Event]
) -> bool:
    """Fire when any single job shows 3 consecutive strictly-rising val_loss
    with the newest/oldest ratio > 1.3.

    We group ``job_epoch`` events per ``job_id``, take the last 3 (sorted by
    timestamp ascending), and check monotonic increase.
    """
    # Build {job_id: [val_loss ordered oldest→newest]} for the last 3 epochs.
    per_job: dict[str, list[float]] = {}
    for ev in reversed(events):  # events is newest-first; reverse → oldest-first
        if ev.event_type != "job_epoch":
            continue
        try:
            data = json.loads(ev.data or "{}")
        except Exception:
            continue
        vl = data.get("val_loss")
        if vl is None:
            continue
        jid = ev.job_id or "__none__"
        per_job.setdefault(jid, [])
        if len(per_job[jid]) < 3:
            per_job[jid].append(float(vl))

    for losses in per_job.values():
        if len(losses) < 3:
            continue
        # losses is oldest→newest (we appended in that order above)
        if losses[1] > losses[0] and losses[2] > losses[1]:
            ratio = losses[2] / losses[0] if losses[0] != 0 else float("inf")
            if ratio > 1.3:
                return True
    return False


def _rule_gpu_idle_during_training(
    batch: Batch, jobs: list[Job], events: list[Event]
) -> bool:
    """Fire when the batch is running, the host's last 3 resource snapshots all
    show gpu_util_pct < 5, and no ``job_done`` event in the last 10 minutes.
    """
    if batch.status != "running":
        return False

    host = batch.host
    if not host:
        return False

    # Check: no job_done in last 10 minutes (using event timestamps)
    ten_min_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
    for ev in events:
        if ev.event_type == "job_done":
            try:
                ts = datetime.fromisoformat(ev.timestamp.replace("Z", "+00:00"))
                if ts >= ten_min_ago:
                    return False  # a job finished recently — not stalled
            except Exception:
                pass

    # The predicate doesn't have direct DB access; ResourceSnapshot rows are
    # pre-loaded via the scan loop into the event list as synthetic entries.
    # We encode recent gpu_util values as ``__gpu_util__`` pseudo-events
    # injected by ``watchdog_loop``. See scan loop below for details.
    gpu_utils: list[float] = []
    for ev in events:
        if ev.event_type == "__gpu_util__":
            try:
                val = float(ev.data or "0")
                gpu_utils.append(val)
            except Exception:
                pass
        if len(gpu_utils) >= 3:
            break

    if len(gpu_utils) < 3:
        return False

    return all(u < 5 for u in gpu_utils)


def _rule_batch_stalled(
    batch: Batch, jobs: list[Job], events: list[Event]
) -> bool:
    """Fire when the last event on the batch is older than
    ``max(5 min, median_epoch_time * 2)``.
    """
    if batch.status != "running":
        return False

    if not events:
        return False

    # Find the most recent event timestamp.
    latest_ts: datetime | None = None
    for ev in events:
        try:
            ts = datetime.fromisoformat(ev.timestamp.replace("Z", "+00:00"))
        except Exception:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts

    if latest_ts is None:
        return False

    # Compute median epoch interval from job_epoch events.
    epoch_times: list[datetime] = []
    for ev in reversed(events):  # oldest → newest
        if ev.event_type == "job_epoch":
            try:
                ts = datetime.fromisoformat(ev.timestamp.replace("Z", "+00:00"))
                epoch_times.append(ts)
            except Exception:
                pass

    median_epoch_s = 60.0  # default 1 min if no epoch data
    if len(epoch_times) >= 2:
        intervals = [
            (epoch_times[i + 1] - epoch_times[i]).total_seconds()
            for i in range(len(epoch_times) - 1)
            if (epoch_times[i + 1] - epoch_times[i]).total_seconds() > 0
        ]
        if intervals:
            median_epoch_s = statistics.median(intervals)

    threshold_s = max(300, median_epoch_s * 2)  # at least 5 minutes

    now = datetime.now(timezone.utc)
    age_s = (now - latest_ts).total_seconds()
    return age_s > threshold_s


def _rule_oom_kill_suspected(
    batch: Batch, jobs: list[Job], events: list[Event]
) -> bool:
    """Fire when any job has status='failed' AND its last log_line event
    mentions out-of-memory keywords (case-insensitive).
    """
    _OOM_RE = re.compile(r"out of memory|CUDA.*allocate", re.IGNORECASE)

    failed_jobs = {j.id for j in jobs if j.status == "failed"}
    if not failed_jobs:
        return False

    # Scan log_line events for failed jobs.
    for ev in events:
        if ev.event_type != "log_line":
            continue
        if ev.job_id not in failed_jobs:
            continue
        try:
            data = json.loads(ev.data or "{}")
        except Exception:
            continue
        line = data.get("line", "") or ""
        if _OOM_RE.search(line):
            return True
    return False


# Registry of all built-in rules (ordered; first match wins nothing — all fire).
BUILTIN_RULES: list[WatchdogRule] = [
    WatchdogRule(
        id="val_loss_diverging",
        name="Val-loss diverging",
        predicate=_rule_val_loss_diverging,
        severity="warn",
        debounce_minutes=30,
        title_tpl="Val-loss diverging on batch {batch_id}",
        body_tpl=(
            "Three consecutive epochs show strictly increasing val_loss "
            "with a ratio > 1.3. Check your learning rate or data pipeline."
        ),
    ),
    WatchdogRule(
        id="gpu_idle_during_training",
        name="GPU idle during training",
        predicate=_rule_gpu_idle_during_training,
        severity="warn",
        debounce_minutes=30,
        title_tpl="GPU idle during training on batch {batch_id}",
        body_tpl=(
            "The last 3 resource snapshots show GPU utilisation < 5% "
            "while the batch is still running. Possible data bottleneck or crash."
        ),
    ),
    WatchdogRule(
        id="batch_stalled",
        name="Batch stalled",
        predicate=_rule_batch_stalled,
        severity="warn",
        debounce_minutes=30,
        title_tpl="Batch {batch_id} appears stalled",
        body_tpl=(
            "No new events for longer than max(5 min, 2× median epoch time). "
            "The process may have hung or been killed."
        ),
    ),
    WatchdogRule(
        id="oom_kill_suspected",
        name="OOM kill suspected",
        predicate=_rule_oom_kill_suspected,
        severity="error",
        debounce_minutes=30,
        title_tpl="OOM kill suspected on batch {batch_id}",
        body_tpl=(
            "A failed job's log output matches 'out of memory' or "
            "'CUDA.*allocate'. Reduce batch size or model size."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Duplicate / debounce check
# ---------------------------------------------------------------------------


async def _already_fired(
    db: AsyncSession,
    rule_id: str,
    batch_id: str,
    debounce_minutes: int,
) -> bool:
    """Return True if the same rule already fired for this batch within the
    debounce window. Imported lazily to avoid circular imports at module load.
    """
    from backend.models import Notification  # noqa: PLC0415

    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=debounce_minutes)
    ).isoformat().replace("+00:00", "Z")

    row = (
        await db.execute(
            select(Notification)
            .where(Notification.rule_id == rule_id)
            .where(Notification.batch_id == batch_id)
            .where(Notification.created_at >= cutoff)
            .limit(1)
        )
    ).scalars().first()
    return row is not None


# ---------------------------------------------------------------------------
# Notification insertion
# ---------------------------------------------------------------------------


def _render(tpl: str, batch_id: str) -> str:
    return tpl.replace("{batch_id}", batch_id)


async def _insert_notification(
    db: AsyncSession,
    rule: WatchdogRule,
    batch: Batch,
    owner_id: int | None,
    admin_ids: list[int],
) -> None:
    """Insert a :class:`Notification` row for the batch owner and every admin.

    Avoids duplicate user_id rows for the same rule+batch if the owner
    happens to also be an admin.
    """
    from backend.models import Notification  # noqa: PLC0415

    title = _render(rule.title_tpl, batch.id)
    body = _render(rule.body_tpl, batch.id)

    now_iso = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    recipients: set[int] = set()
    if owner_id is not None:
        recipients.add(owner_id)
    recipients.update(admin_ids)

    for uid in recipients:
        row = Notification(
            user_id=uid,
            batch_id=batch.id,
            rule_id=rule.id,
            severity=rule.severity,
            title=title,
            body=body,
            created_at=now_iso,
            read_at=None,
        )
        db.add(row)

    await db.commit()
    log.info(
        "watchdog: inserted notification rule=%s batch=%s recipients=%s",
        rule.id,
        batch.id,
        recipients,
    )


# ---------------------------------------------------------------------------
# Feishu fire-and-forget (error severity only)
# ---------------------------------------------------------------------------


async def _maybe_fire_feishu(rule: WatchdogRule, batch_id: str) -> None:
    webhook = os.environ.get("ARGUS_FEISHU_WEBHOOK")
    if not webhook or rule.severity != "error":
        return
    try:
        import httpx  # noqa: PLC0415 - optional dep, import late

        title = _render(rule.title_tpl, batch_id)
        body = _render(rule.body_tpl, batch_id)
        payload = {
            "msg_type": "text",
            "content": {"text": f"[{rule.severity.upper()}] {title}\n{body}"},
        }
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(webhook, json=payload)
    except Exception as exc:  # noqa: BLE001
        log.debug("feishu fire-and-forget failed: %s", exc)


# ---------------------------------------------------------------------------
# Team-A guardrails: divergence + idle-job detectors (roadmap #12 / #13).
#
# These run alongside the classic rule engine but own their own side
# effects: they mutate ``batch.status`` / ``job.is_idle_flagged`` and
# emit synthetic ``batch_diverged`` / ``job_idle_flagged`` events into
# the event log so the SSE firehose + retrospective analytics pick them
# up.  They intentionally skip the Notification table (handled by the
# existing ``val_loss_diverging`` / ``gpu_idle_during_training`` rules
# above) so we don't double-up the in-app bell.
# ---------------------------------------------------------------------------


def _now_iso_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


async def _check_batch_divergence(
    db: AsyncSession, batch: Batch, events: list[Event]
) -> bool:
    """Flag a batch as divergent when val_loss explodes or NaNs.

    Criteria (OR):
      * Any ``job_epoch`` event with ``val_loss`` in {NaN, +Inf, -Inf}.
      * ``val_loss`` strictly increased over the last ``divergence_window``
        epochs AND newest / oldest ratio ≥ ``divergence_ratio``.

    Side effects (on a match):
      * Set ``batch.status = 'divergent'`` (persisted via the caller's
        session — the watchdog loop commits once per scan).
      * Insert a ``batch_diverged`` event row so SSE subscribers and
        downstream analytics observe the flip.

    A batch already at ``status='divergent'`` short-circuits and returns
    False so repeated scans don't spam duplicate events.
    """
    if batch.status == "divergent":
        return False

    settings = get_settings()
    window = max(2, int(settings.divergence_window))
    ratio_threshold = max(1.0, float(settings.divergence_ratio))

    # Build {job_id: [val_loss oldest → newest]} from the event tail.
    per_job: dict[str, list[float]] = {}
    for ev in reversed(events):  # events arrive newest-first → reverse
        if ev.event_type != "job_epoch":
            continue
        try:
            data = json.loads(ev.data or "{}")
        except Exception:
            continue
        vl = data.get("val_loss")
        if vl is None:
            continue
        try:
            vlf = float(vl)
        except (TypeError, ValueError):
            continue
        per_job.setdefault(ev.job_id or "__none__", []).append(vlf)

    # 1) NaN / Inf short-circuit.
    for losses in per_job.values():
        if any(math.isnan(v) or math.isinf(v) for v in losses):
            await _mark_divergent(
                db, batch, reason="nan_or_inf", ratio=None, window=window
            )
            return True

    # 2) Strictly-monotonic growth by ≥ ``ratio_threshold`` over ``window``.
    for losses in per_job.values():
        if len(losses) < window:
            continue
        tail = losses[-window:]
        if not all(tail[i + 1] > tail[i] for i in range(window - 1)):
            continue
        if tail[0] <= 0:
            # Ratio from zero/negative is undefined; skip to avoid
            # false alarms. Pure positive growth is the interesting case.
            continue
        ratio = tail[-1] / tail[0]
        if ratio >= ratio_threshold:
            await _mark_divergent(
                db, batch, reason="ratio", ratio=ratio, window=window
            )
            return True

    return False


async def _mark_divergent(
    db: AsyncSession,
    batch: Batch,
    *,
    reason: str,
    ratio: float | None,
    window: int,
) -> None:
    """Persist ``status='divergent'`` and emit a batch_diverged event."""
    batch.status = "divergent"
    payload = {
        "reason": reason,
        "ratio": ratio,
        "window": window,
        "locale_key": "guardrails.batch.diverged.title",
    }
    ev = Event(
        batch_id=batch.id,
        job_id=None,
        event_type="batch_diverged",
        timestamp=_now_iso_utc(),
        schema_version="1.1",
        data=json.dumps(payload, separators=(",", ":")),
    )
    db.add(ev)
    log.info(
        "guardrails: batch %s flagged divergent reason=%s ratio=%s window=%s",
        batch.id,
        reason,
        ratio,
        window,
    )
    # Best-effort email dispatch. Failures must never break the watchdog scan.
    try:
        from backend.services.notifications_dispatcher import (
            dispatch_email_for_event,
        )
        await dispatch_email_for_event(
            db, event_type="batch_diverged", batch=batch
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("watchdog: batch_diverged email dispatch failed: %r", exc)


async def _check_stalled_batches(
    db: AsyncSession, settings
) -> list[Batch]:
    """Flip batches to ``status='stalled'`` when no events arrived recently.

    Criterion: the batch is in ``('running', 'stopping')`` AND the newest
    of (latest Event, latest ResourceSnapshot) is older than
    ``settings.stall_timeout_min`` minutes.

    Rationale: ctrl-C / machine reboot / OOM-kill all leave the Batch row
    at ``status='running'`` forever because no ``batch_done`` event ever
    arrives. Other users scanning the dashboard still see "running" and
    wait for it indefinitely. Flipping to ``'stalled'`` gives the UI a
    clear heartbeat-lost signal while remaining a non-terminal state —
    the reporter can resume and the next legitimate event will bring the
    batch back to ``'running'``.

    Side effects (on a match):
      * Set ``batch.status = 'stalled'`` (persisted by the caller's
        commit at the end of the scan).
      * Insert a ``batch_stalled`` Event row with payload
        ``{"last_event_at": iso, "minutes_since": N}`` so SSE
        subscribers and audit analytics observe the flip.

    Terminal statuses (``done``, ``failed``, ``divergent``,
    ``stopped``) are skipped — a stalled flag on a finished batch makes
    no sense. Already-stalled batches short-circuit to avoid duplicate
    events.
    """
    timeout_min = max(1, int(settings.stall_timeout_min))
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(minutes=timeout_min)
    ).isoformat().replace("+00:00", "Z")

    # Only actively-live statuses are candidates. ``stalled`` batches are
    # excluded deliberately — re-firing the event every scan spams the
    # activity feed. A batch exits 'stalled' the moment the reporter
    # posts another event (handled by the ingestion path, not here).
    candidates = list(
        (
            await db.execute(
                select(Batch).where(Batch.status.in_(("running", "stopping")))
            )
        ).scalars().all()
    )
    if not candidates:
        return []

    flipped: list[Batch] = []
    now = datetime.now(timezone.utc)

    for batch in candidates:
        # Latest Event timestamp (newest-first).
        last_ev_ts = (
            await db.execute(
                select(Event.timestamp)
                .where(Event.batch_id == batch.id)
                .order_by(Event.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        # Latest ResourceSnapshot timestamp (fallback signal when the
        # reporter only streams telemetry without events).
        last_snap_ts = (
            await db.execute(
                select(ResourceSnapshot.timestamp)
                .where(ResourceSnapshot.batch_id == batch.id)
                .order_by(ResourceSnapshot.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        latest_iso = max(
            filter(None, (last_ev_ts, last_snap_ts)),
            default=None,
        )
        # No activity at all → fall back to batch.start_time so we still
        # flag zombie runs that crashed before their first event landed.
        if latest_iso is None:
            latest_iso = batch.start_time
        if latest_iso is None:
            # Truly no timestamp we can reason about; leave alone.
            continue

        if latest_iso >= cutoff_iso:
            continue

        try:
            latest_dt = datetime.fromisoformat(latest_iso.replace("Z", "+00:00"))
        except ValueError:
            continue

        minutes_since = int((now - latest_dt).total_seconds() // 60)
        batch.status = "stalled"
        payload = {
            "last_event_at": latest_iso,
            "minutes_since": minutes_since,
            "locale_key": "guardrails.batch.stalled.title",
        }
        db.add(
            Event(
                batch_id=batch.id,
                job_id=None,
                event_type="batch_stalled",
                timestamp=_now_iso_utc(),
                schema_version="1.1",
                data=json.dumps(payload, separators=(",", ":")),
            )
        )
        flipped.append(batch)
        log.info(
            "guardrails: batch %s flagged stalled (last_event=%s, minutes_since=%s)",
            batch.id,
            latest_iso,
            minutes_since,
        )

    return flipped


async def _check_idle_jobs(
    db: AsyncSession, batch: Batch
) -> list[Job]:
    """Flag running jobs whose recent GPU util stays below 5%.

    "Recent" means the newest :class:`ResourceSnapshot` rows tied to the
    batch with timestamps spanning at least
    ``idle_job_threshold_min`` minutes.

    Side effects (on a match):
      * Set ``job.is_idle_flagged = True`` (idempotent on the flag — but
        a recovery transition from True back to False emits a
        ``job_idle_recovered`` event so subscribers can clear UI state).
      * Insert one ``job_idle_flagged`` event per flipped job.
      * Insert one ``job_idle_recovered`` event per cleared job when the
        latest sample shows GPU util > 5%.

    Does NOT kill the job — advisory only per roadmap #13.
    Returns the list of jobs freshly flagged in this pass.

    Bug-fix history (cleanup PR):
      * (a) Jobs with NULL status were silently excluded from the
        candidate set because ``in_((..., None))`` is filtered out by
        SQL's three-valued logic. Switched to an explicit
        ``OR (status IS NULL)`` predicate.
      * (b) The flag was sticky: once flipped to True it never cleared.
        We now run a recovery pass on already-flagged jobs and flip
        back to False when the newest sample shows util > 5%.
      * (c) Per-job filtering: ``ResourceSnapshot`` does not have a
        ``job_id`` column on this branch (see ``models.py``), so we
        cannot scope the GPU-util window to a single job. We keep the
        per-batch fallback and document the limitation here. When a
        future migration adds ``ResourceSnapshot.job_id`` the query
        below should add ``.where(ResourceSnapshot.job_id == job.id)``.
    """
    settings = get_settings()
    window_min = max(1, int(settings.idle_job_threshold_min))
    cutoff_ts = (
        datetime.now(timezone.utc) - timedelta(minutes=window_min)
    ).isoformat().replace("+00:00", "Z")

    # (a) NULL-status fix: ``Job.status.in_((..., None))`` does not match
    # NULL rows under SQL's three-valued logic. Use an explicit
    # ``IS NULL`` branch via ``or_``.
    candidate_jobs = list(
        (
            await db.execute(
                select(Job)
                .where(Job.batch_id == batch.id)
                .where(
                    or_(
                        Job.status.is_(None),
                        Job.status.in_(("running", "RUNNING")),
                    )
                )
            )
        ).scalars().all()
    )
    if not candidate_jobs:
        return []

    # Pull the per-batch snapshot window once — the per-job loops below
    # all share the same set because ResourceSnapshot lacks ``job_id``.
    # (c) Per-job scoping limitation documented in the docstring.
    batch_snaps = list(
        (
            await db.execute(
                select(ResourceSnapshot)
                .where(ResourceSnapshot.batch_id == batch.id)
                .where(ResourceSnapshot.timestamp >= cutoff_ts)
                .order_by(ResourceSnapshot.timestamp.desc())
            )
        ).scalars().all()
    )

    flipped: list[Job] = []
    for job in candidate_jobs:
        snaps = batch_snaps  # see (c) — per-batch fallback
        if len(snaps) < 2:
            continue
        try:
            oldest_ts = datetime.fromisoformat(
                snaps[-1].timestamp.rstrip("Z")
            ).replace(tzinfo=timezone.utc)
            newest_ts = datetime.fromisoformat(
                snaps[0].timestamp.rstrip("Z")
            ).replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        # Require at least 90% of the advertised window so a
        # thin-sampled job still qualifies.
        if (newest_ts - oldest_ts).total_seconds() < window_min * 60 * 0.9:
            continue

        utils = [s.gpu_util_pct for s in snaps if s.gpu_util_pct is not None]
        if not utils:
            continue

        # (b) Recovery branch: an already-flagged job whose newest sample
        # shows util > 5% is back to work — clear the flag and emit a
        # recovery event so the UI / SSE bell can reset.
        if job.is_idle_flagged:
            newest_util = utils[0]  # snaps were ordered desc by timestamp
            if newest_util > 5.0:
                job.is_idle_flagged = False
                payload = {
                    "job_id": job.id,
                    "newest_util_pct": newest_util,
                    "locale_key": "guardrails.job.idle.recovered",
                }
                db.add(
                    Event(
                        batch_id=batch.id,
                        job_id=job.id,
                        event_type="job_idle_recovered",
                        timestamp=_now_iso_utc(),
                        schema_version="1.1",
                        data=json.dumps(payload, separators=(",", ":")),
                    )
                )
                log.info(
                    "guardrails: job %s/%s idle flag CLEARED (util=%.1f%%)",
                    batch.id,
                    job.id,
                    newest_util,
                )
            # Whether we cleared or stayed flagged, do not re-flag in the
            # same pass.
            continue

        # Fresh flag-on path: every recent sample below the threshold.
        if all(u < 5.0 for u in utils):
            job.is_idle_flagged = True
            payload = {
                "job_id": job.id,
                "minutes": window_min,
                "locale_key": "guardrails.job.idle.title",
            }
            db.add(
                Event(
                    batch_id=batch.id,
                    job_id=job.id,
                    event_type="job_idle_flagged",
                    timestamp=_now_iso_utc(),
                    schema_version="1.1",
                    data=json.dumps(payload, separators=(",", ":")),
                )
            )
            flipped.append(job)
            log.info(
                "guardrails: job %s/%s flagged idle window_min=%s",
                batch.id,
                job.id,
                window_min,
            )
            # Best-effort email dispatch per flipped job.
            try:
                from backend.services.notifications_dispatcher import (
                    dispatch_email_for_event,
                )
                await dispatch_email_for_event(
                    db,
                    event_type="job_idle_flagged",
                    batch=batch,
                    job=job,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "watchdog: job_idle_flagged email dispatch failed: %r",
                    exc,
                )

    return flipped


# ---------------------------------------------------------------------------
# Scan loop
# ---------------------------------------------------------------------------


async def watchdog_loop_once(db: AsyncSession) -> None:
    """Run one watchdog scan across all running batches.

    Called every 60 s from ``watchdog_loop``. Also callable directly in
    tests.
    """
    # Load all running batches.
    running = (
        await db.execute(
            select(Batch).where(Batch.status == "running")
        )
    ).scalars().all()

    if not running:
        return

    # Preload admin user IDs once per scan.
    admin_ids: list[int] = list(
        (
            await db.execute(
                select(User.id).where(User.is_admin.is_(True))
            )
        ).scalars().all()
    )

    for batch in running:
        # Fetch jobs for this batch.
        jobs = list(
            (
                await db.execute(
                    select(Job).where(Job.batch_id == batch.id)
                )
            ).scalars().all()
        )

        # Fetch last 50 events (newest first).
        events = list(
            (
                await db.execute(
                    select(Event)
                    .where(Event.batch_id == batch.id)
                    .order_by(Event.timestamp.desc())
                    .limit(50)
                )
            ).scalars().all()
        )

        # Inject synthetic gpu_util pseudo-events so the gpu_idle rule
        # can inspect resource snapshots without DB access in the predicate.
        if batch.host:
            snapshots = list(
                (
                    await db.execute(
                        select(ResourceSnapshot)
                        .where(ResourceSnapshot.host == batch.host)
                        .where(ResourceSnapshot.batch_id == batch.id)
                        .order_by(ResourceSnapshot.timestamp.desc())
                        .limit(3)
                    )
                ).scalars().all()
            )
            for snap in snapshots:
                if snap.gpu_util_pct is not None:
                    fake = Event(
                        batch_id=batch.id,
                        event_type="__gpu_util__",
                        timestamp=snap.timestamp,
                        schema_version="internal",
                        data=str(snap.gpu_util_pct),
                    )
                    events.insert(0, fake)

        # Evaluate each rule.
        for rule in BUILTIN_RULES:
            try:
                fired = rule.predicate(batch, jobs, events)
            except Exception:  # noqa: BLE001
                log.exception("watchdog rule %s raised", rule.id)
                continue

            if not fired:
                continue

            if await _already_fired(db, rule.id, batch.id, rule.debounce_minutes):
                log.debug(
                    "watchdog: debouncing rule=%s batch=%s", rule.id, batch.id
                )
                continue

            await _insert_notification(
                db, rule, batch, batch.owner_id, admin_ids
            )
            # Fire-and-forget Feishu for error-severity rules.
            asyncio.create_task(  # noqa: RUF006
                _maybe_fire_feishu(rule, batch.id)
            )

        # Team-A guardrails: divergence + idle-job detectors run after
        # the classic rule engine so any Notification rows they leave
        # behind are still debounced via the same table. These ALSO
        # mutate batch.status / job.is_idle_flagged directly — hence a
        # commit at the end.
        try:
            await _check_batch_divergence(db, batch, events)
        except Exception:  # noqa: BLE001
            log.exception(
                "guardrails: divergence check failed for batch %s", batch.id
            )
        try:
            await _check_idle_jobs(db, batch)
        except Exception:  # noqa: BLE001
            log.exception(
                "guardrails: idle-job check failed for batch %s", batch.id
            )

    # Stalled-batch sweep runs once per scan across *all* candidate
    # batches (not scoped to the ``running`` loop above, because we also
    # want to catch ``stopping`` batches whose stop request was
    # acknowledged but never completed).
    try:
        await _check_stalled_batches(db, get_settings())
    except Exception:  # noqa: BLE001
        log.exception("guardrails: stalled-batch check failed")

    # Persist divergence / idle-flag / stalled side effects in one
    # commit per scan.
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("guardrails: commit after scan failed")


async def watchdog_loop() -> None:
    """Background task: runs ``watchdog_loop_once`` every 60 seconds.

    Cancelled on app shutdown (lifespan). Each iteration opens its own
    session so a transient DB error does not kill the loop.
    """
    while True:
        try:
            async with SessionLocal() as db:
                await watchdog_loop_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("watchdog scan failed")
        await asyncio.sleep(60)
