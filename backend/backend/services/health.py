"""Batch health assessment helper.

Pure-ish function that reads the recent events for a batch and reports:

* ``is_stalled`` — no event in the last N seconds, for any non-terminal
  batch. The signal is independent of whether the watchdog has already
  flipped ``batch.status`` to ``stalled``: the UI uses this flag to drive
  the pulsing-yellow indicator while the operator decides what to do.
  Terminal statuses (``done`` / ``failed`` / ``cancelled``) are never
  flagged stalled — finished work cannot be stalled.
* ``last_event_age_s`` — wall-clock gap between ``now`` and the freshest
  event timestamp (``None`` if no events at all)
* ``failure_count`` — number of ``job_failed`` / ``batch_failed`` events
* ``warnings`` — short human-readable strings the UI can render as
  tags (``"no events in 8m"``, ``"3 jobs failed"``, etc.)

The threshold defaults to 300 seconds (§16.5); callers that want to
respect the admin-toggleable ``stalled_threshold_sec`` flag should
resolve the flag first and pass it in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Batch, Event

log = logging.getLogger(__name__)


# Statuses that mean "this batch is finished, do not flag stalled".
# Module-scope (instead of per-call) because the set is immutable, the
# membership check fires on every health probe, and tests benefit from
# being able to import the canonical constant rather than re-typing
# string literals.
#
# Note: ``stopping`` is *not* in this set on purpose. A batch flipped
# to ``stopping`` is still expected to emit shutdown events; if those
# stop arriving for ``stalled_threshold_s`` we still want the UI to
# pulse yellow so the operator notices the cleanup got wedged. If we
# ever decide ``stopping`` should be silent during shutdown, add it
# here and document the change in the divergent-terminal-status v0.1.5
# decision.
_TERMINAL: frozenset[str] = frozenset({"done", "failed", "cancelled"})


def _parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse tolerant of the ``Z`` suffix."""
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        log.debug("failed to parse timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def batch_health(
    batch_id: str,
    db: AsyncSession,
    stalled_threshold_s: int = 300,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute liveness signals for one batch.

    Parameters
    ----------
    batch_id:
        Target batch. If it doesn't exist (or is soft-deleted), returns
        a "missing" skeleton rather than raising — callers decide
        whether that means 404.
    db:
        Active async session.
    stalled_threshold_s:
        Seconds of silence that tip a running batch into "stalled".
    now:
        Optional override for deterministic testing. Defaults to
        ``datetime.now(utc)``.
    """
    reference_now = now or datetime.now(timezone.utc)

    batch = await db.get(Batch, batch_id)
    if batch is None or batch.is_deleted:
        return {
            "batch_id": batch_id,
            "is_stalled": False,
            "last_event_age_s": None,
            "failure_count": 0,
            "warnings": ["batch_missing"],
            "stalled_threshold_s": stalled_threshold_s,
        }

    # Freshest event timestamp + failure count in two cheap queries.
    latest_ts = (
        await db.execute(
            select(func.max(Event.timestamp)).where(
                Event.batch_id == batch_id
            )
        )
    ).scalar_one_or_none()

    fail_count = (
        await db.execute(
            select(func.count(Event.id)).where(
                Event.batch_id == batch_id,
                Event.event_type.in_(("job_failed", "batch_failed")),
            )
        )
    ).scalar_one()

    last_event_dt = _parse_iso(latest_ts)
    last_event_age_s: int | None
    if last_event_dt is None:
        last_event_age_s = None
    else:
        delta = (reference_now - last_event_dt).total_seconds()
        last_event_age_s = max(0, int(delta))

    # is_stalled is true whenever the watchdog window has passed without
    # events, independent of whether ``batch.status`` has been auto-flipped
    # to ``stalled`` already. (Argus consistency check, 2026-04-25:
    # short-circuiting on status='running' caused already-stalled batches
    # to display as healthy in the UI.)
    #
    # Terminal statuses are excluded — finished work is not stalled
    # regardless of how long ago the last event landed.  ``status=None``
    # (just-created row that never received its first ``batch_start``)
    # falls through to the empty-string + ``not in _TERMINAL`` branch,
    # so a never-started batch with stale events does flag stalled.
    effective_status = (batch.status or "").lower()
    is_stalled = bool(
        effective_status not in _TERMINAL
        and last_event_age_s is not None
        and last_event_age_s > stalled_threshold_s
    )

    warnings: list[str] = []
    if is_stalled and last_event_age_s is not None:
        mins = last_event_age_s // 60
        warnings.append(f"no events in {mins}m")
    if fail_count:
        warnings.append(
            f"{fail_count} failure event{'s' if fail_count != 1 else ''}"
        )

    return {
        "batch_id": batch_id,
        "is_stalled": is_stalled,
        "last_event_age_s": last_event_age_s,
        "failure_count": int(fail_count or 0),
        "warnings": warnings,
        "stalled_threshold_s": stalled_threshold_s,
    }
