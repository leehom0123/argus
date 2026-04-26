"""ETA estimation helpers.

The requirements §16.5 call for an EMA over the last ~10 completed
jobs' ``elapsed_s`` times the number of still-pending jobs. This module
exposes a single pure function so it can be reused from the dashboard
service, the batch-eta endpoint, and the project-card renderer without
dragging in a DB session.

``compute_job_eta`` is the per-job estimation helper reused from both
``GET /api/jobs/{b}/{j}/eta`` and ``GET /api/batches/{id}/jobs/eta-all``.
"""
from __future__ import annotations

import logging
import math
from typing import Iterable, NamedTuple, Optional, Sequence

logger = logging.getLogger(__name__)

_FALLBACK_EPOCHS_TOTAL = 50


class JobEtaResult(NamedTuple):
    """Value object returned by :func:`compute_job_eta`."""

    job_id: str
    elapsed_s: int
    epochs_done: int
    epochs_total: int
    avg_epoch_time_s: float | None
    eta_s: int | None
    eta_iso: str | None


def compute_job_eta(
    job_id: str,
    job_start_iso: str | None,
    train_epochs_config: int | None,
    epoch_timestamps: Sequence[str],
    now_iso: str | None = None,
) -> JobEtaResult:
    """Estimate per-job ETA from ``job_epoch`` event timestamps.

    Parameters
    ----------
    job_id:
        Identifier of the job (echoed back in the result).
    job_start_iso:
        ISO-8601 timestamp when the job started (``Job.start_time``).
        Used only for ``elapsed_s``.
    train_epochs_config:
        Total epochs from ``job.metrics`` / hyperparams.  Falls back to
        :data:`_FALLBACK_EPOCHS_TOTAL` (50) with a warning when ``None``.
    epoch_timestamps:
        Chronologically ordered ISO-8601 timestamps of ``job_epoch`` events
        for this job (oldest first).  At least 2 are needed for a meaningful
        average; fewer returns ``eta_s=None`` ("warming up").
    now_iso:
        Override "now" for testing.  Defaults to :func:`datetime.now`.

    Returns
    -------
    JobEtaResult
        All fields needed for the API response.
    """
    from datetime import datetime, timezone

    def _parse(ts: str | None) -> datetime | None:
        if not ts:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00"):
            try:
                return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    now_dt = _parse(now_iso) or datetime.now(timezone.utc)
    start_dt = _parse(job_start_iso)

    elapsed_s = 0
    if start_dt is not None:
        elapsed_s = max(0, int((now_dt - start_dt).total_seconds()))

    # Resolve total epochs
    if train_epochs_config is not None and train_epochs_config > 0:
        epochs_total = train_epochs_config
    else:
        logger.warning(
            "job %s: train_epochs not set; falling back to %d",
            job_id,
            _FALLBACK_EPOCHS_TOTAL,
        )
        epochs_total = _FALLBACK_EPOCHS_TOTAL

    # epochs_done = number of epoch events received
    epochs_done = len(epoch_timestamps)

    # Need at least 2 epoch events to compute a meaningful per-epoch time
    if epochs_done < 2:
        return JobEtaResult(
            job_id=job_id,
            elapsed_s=elapsed_s,
            epochs_done=epochs_done,
            epochs_total=epochs_total,
            avg_epoch_time_s=None,
            eta_s=None,
            eta_iso=None,
        )

    first_dt = _parse(epoch_timestamps[0])
    last_dt = _parse(epoch_timestamps[-1])
    if first_dt is None or last_dt is None:
        return JobEtaResult(
            job_id=job_id,
            elapsed_s=elapsed_s,
            epochs_done=epochs_done,
            epochs_total=epochs_total,
            avg_epoch_time_s=None,
            eta_s=None,
            eta_iso=None,
        )

    span_s = max(0.0, (last_dt - first_dt).total_seconds())
    avg_epoch_time_s = span_s / max(1, epochs_done - 1)

    remaining = max(0, epochs_total - epochs_done)
    eta_s = int(math.ceil(remaining * avg_epoch_time_s))

    eta_iso: str | None = None
    if remaining > 0:
        from datetime import timedelta

        finish_dt = now_dt + timedelta(seconds=eta_s)
        eta_iso = finish_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        eta_s = 0

    return JobEtaResult(
        job_id=job_id,
        elapsed_s=elapsed_s,
        epochs_done=epochs_done,
        epochs_total=epochs_total,
        avg_epoch_time_s=round(avg_epoch_time_s, 3) if avg_epoch_time_s else None,
        eta_s=eta_s,
        eta_iso=eta_iso,
    )


def ema_eta(
    done_elapsed_list: Iterable[int | float],
    pending_count: int,
    alpha: float = 0.3,
) -> Optional[int]:
    """Estimate remaining seconds via EMA × pending count.

    Parameters
    ----------
    done_elapsed_list:
        ``elapsed_s`` for the most recent done jobs — **newest first**.
        We iterate in reverse so the classical EMA recurrence
        ``ema_{t} = α·x_{t} + (1-α)·ema_{t-1}`` puts more weight on the
        fresher samples.
    pending_count:
        Number of jobs still to run. ``0`` short-circuits to ``0``
        (nothing pending means ETA is zero).
    alpha:
        EMA smoothing factor in (0, 1]. Higher values respond faster
        to recent changes.

    Returns
    -------
    Optional[int]
        ``None`` if we have no sample data at all *and* work is pending;
        ``0`` if nothing is pending; rounded seconds otherwise.
    """
    if pending_count <= 0:
        return 0

    samples = [float(x) for x in done_elapsed_list if x is not None and x >= 0]
    if not samples:
        return None

    # Oldest first so the most recent sample (last) dominates the EMA
    # — this matches how ``elapsed_s`` arrives in descending time order
    # from the caller (newest first); we reverse to oldest-first.
    ordered = list(reversed(samples))
    ema = ordered[0]
    for value in ordered[1:]:
        ema = alpha * value + (1.0 - alpha) * ema

    return int(round(ema * pending_count))
