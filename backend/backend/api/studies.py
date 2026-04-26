"""``/api/studies`` — Optuna multirun visualisation (v0.2 hyperopt-ui).

A *study* is the natural grouping of jobs sharing the same
``optuna.study_name`` label, emitted by Sibyl's ``Monitor`` callback
under Hydra's optuna sweeper. The label survives in
``Job.extra.optuna`` because the ``job_start`` event handler stashes
every non-(model/dataset) field there (see ``api/events.py``).

This module never reaches into raw Optuna SQLite — it derives every
metric from Argus's own job rows so RBAC and visibility flow through
the same :class:`VisibilityResolver` we use everywhere else. Trials
from batches a user cannot see are filtered out before any
aggregation; if the visible set is empty we return an empty list
rather than 404 (the FE renders an empty-state).

Three routes:

* ``GET /api/studies``                    — list distinct studies + summary
* ``GET /api/studies/{name}``             — one study's trials (sortable on FE)
* ``GET /api/studies/{name}/trials/{id}`` — single trial detail
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Batch, Job, User
from backend.schemas.studies import (
    StudyDetailOut,
    StudyListOut,
    StudySummary,
    TrialDetailOut,
    TrialRow,
)
from backend.services.visibility import VisibilityResolver

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/studies", tags=["studies"])

# Default headline metric when the reporter doesn't tag the trial with
# an explicit ``optuna.target_metric``. MSE is the dominant forecast
# loss across the time-series benchmark, so the leaderboard already
# treats it as the canonical column.
_DEFAULT_METRIC = "MSE"


def _safe_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_optuna(extra: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the ``optuna`` block out of ``Job.extra`` (or None).

    Sibyl's ``Monitor`` emits this on ``job_start``::

        {"optuna": {"study_name": "...", "trial_id": 0, "params": {...},
                    "direction": "minimize", "sampler": "TPESampler",
                    "target_metric": "val_loss"}}

    Only ``study_name`` and ``trial_id`` are required — everything
    else is best-effort metadata.
    """
    o = extra.get("optuna")
    if not isinstance(o, dict):
        return None
    if not o.get("study_name"):
        return None
    return o


def _coerce_float(v: Any) -> float | None:
    """JSON numbers may decode as int/str/None; clamp to float or None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _trial_value(
    metrics: dict[str, Any],
    target_metric: str | None,
) -> tuple[float | None, str | None]:
    """Pick the headline metric for a trial.

    Order of preference:
      1. ``optuna.target_metric`` if the reporter pinned one and it
         exists in ``metrics``
      2. ``MSE`` (canonical loss for time-series)
      3. The first numeric key, deterministic by sort order
    """
    if target_metric and target_metric in metrics:
        v = _coerce_float(metrics.get(target_metric))
        if v is not None:
            return v, target_metric

    if _DEFAULT_METRIC in metrics:
        v = _coerce_float(metrics.get(_DEFAULT_METRIC))
        if v is not None:
            return v, _DEFAULT_METRIC

    for k in sorted(metrics.keys()):
        v = _coerce_float(metrics.get(k))
        if v is not None:
            return v, k

    return None, None


def _is_better(
    candidate: float, incumbent: float, direction: str | None
) -> bool:
    """``minimize`` (default for forecast loss) → smaller is better."""
    if direction and direction.lower() == "maximize":
        return candidate > incumbent
    return candidate < incumbent


async def _visible_jobs(
    user: User, db: AsyncSession
) -> list[tuple[Job, Batch]]:
    """Return every (job, batch) pair the user is allowed to read.

    We first compute the visible-batch SELECT (RBAC + soft-delete +
    demo filter), then JOIN jobs onto it. Going through the resolver
    keeps the visibility rule co-located with every other read path.
    """
    resolver = VisibilityResolver()
    batches_stmt = await resolver.visible_batches_query(user, "all", db=db)
    visible_batch_subq = batches_stmt.subquery()

    # SQLAlchemy needs an explicit join condition because the
    # subquery wraps the SELECT; we reference Batch.id by column
    # path off the subquery.
    stmt = (
        select(Job, Batch)
        .join(Batch, Job.batch_id == Batch.id)
        .join(
            visible_batch_subq,
            visible_batch_subq.c.id == Job.batch_id,
        )
        .where(Job.is_deleted.is_(False))
    )
    rows = (await db.execute(stmt)).all()
    return [(j, b) for j, b in rows]


# ---------------------------------------------------------------------------
# GET /api/studies
# ---------------------------------------------------------------------------
@router.get("", response_model=StudyListOut)
async def list_studies(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StudyListOut:
    """Return every distinct study the caller can see, newest first.

    Each row aggregates over the trials (jobs) tagged with that
    ``study_name``. ``best_value`` follows the recorded ``direction``;
    when no trial reports the metric we leave it ``null`` so the FE
    can render a "running" placeholder.
    """
    pairs = await _visible_jobs(user, db)

    # study_name -> aggregator dict
    agg: dict[str, dict[str, Any]] = {}

    for job, _batch in pairs:
        extra = _safe_loads(job.extra)
        opt = _extract_optuna(extra)
        if opt is None:
            continue

        name = str(opt["study_name"])
        slot = agg.setdefault(
            name,
            {
                "n_trials": 0,
                "n_done": 0,
                "n_failed": 0,
                "best_value": None,
                "best_metric": None,
                "direction": opt.get("direction"),
                "sampler": opt.get("sampler"),
                "last_run": None,
            },
        )
        slot["n_trials"] += 1
        status = (job.status or "").lower()
        if status == "done":
            slot["n_done"] += 1
        elif status == "failed":
            slot["n_failed"] += 1

        # Direction / sampler: first non-null wins (immutable on study).
        if slot["direction"] is None and opt.get("direction"):
            slot["direction"] = opt.get("direction")
        if slot["sampler"] is None and opt.get("sampler"):
            slot["sampler"] = opt.get("sampler")

        # Newest start_time wins for ``last_run``.
        if job.start_time:
            cur = slot["last_run"]
            if cur is None or job.start_time > cur:
                slot["last_run"] = job.start_time

        # Best value derived from job metrics.
        metrics = _safe_loads(job.metrics)
        target = opt.get("target_metric")
        v, mkey = _trial_value(metrics, target)
        if v is not None:
            cur_best = slot["best_value"]
            if cur_best is None or _is_better(v, cur_best, slot["direction"]):
                slot["best_value"] = v
                slot["best_metric"] = mkey

    items = [
        StudySummary(
            study_name=name,
            n_trials=slot["n_trials"],
            n_done=slot["n_done"],
            n_failed=slot["n_failed"],
            best_value=slot["best_value"],
            best_metric=slot["best_metric"],
            direction=slot["direction"],
            sampler=slot["sampler"],
            last_run=slot["last_run"],
        )
        for name, slot in agg.items()
    ]

    # Sort newest-run first; null last_run sinks to the bottom.
    items.sort(
        key=lambda s: (s.last_run is None, s.last_run or ""),
        reverse=False,
    )
    items.reverse()  # equivalent to "non-null DESC, null last"
    # The double-flip above is intentional: tuple sort is ascending,
    # we want non-null DESC + null at end. Reversing makes non-null
    # DESC but pushes nulls to the front, so we re-shuffle.
    nonnull = [x for x in items if x.last_run is not None]
    nulls = [x for x in items if x.last_run is None]
    nonnull.sort(key=lambda s: s.last_run or "", reverse=True)
    return StudyListOut(studies=nonnull + nulls)


# ---------------------------------------------------------------------------
# GET /api/studies/{name}
# ---------------------------------------------------------------------------
@router.get("/{study_name}", response_model=StudyDetailOut)
async def get_study(
    study_name: str,
    sort: str = Query(
        default="value",
        description="Sort key: ``value`` (asc by default), ``trial_id``, ``start_time``.",
    ),
    order: str = Query(
        default="asc",
        description="``asc`` | ``desc``. Direction-aware for ``value`` when omitted.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> StudyDetailOut:
    """Return all trials for a study + the union of param/metric keys.

    The frontend uses ``param_keys`` to render sortable hyperparameter
    columns and ``metric_keys`` for the metric column drop-down.
    """
    if sort not in {"value", "trial_id", "start_time"}:
        raise HTTPException(status_code=400, detail=tr(locale, "study.invalid_sort"))
    if order not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail=tr(locale, "study.invalid_order"))

    pairs = await _visible_jobs(user, db)

    direction: str | None = None
    sampler: str | None = None
    target_metric: str | None = None
    rows: list[TrialRow] = []
    param_keys: set[str] = set()
    metric_keys: set[str] = set()
    n_done = 0
    n_failed = 0
    best_value: float | None = None
    best_metric: str | None = None

    for job, _batch in pairs:
        extra = _safe_loads(job.extra)
        opt = _extract_optuna(extra)
        if opt is None or str(opt["study_name"]) != study_name:
            continue

        if direction is None and opt.get("direction"):
            direction = opt.get("direction")
        if sampler is None and opt.get("sampler"):
            sampler = opt.get("sampler")
        if target_metric is None and opt.get("target_metric"):
            target_metric = opt.get("target_metric")

        params = opt.get("params") or {}
        if isinstance(params, dict):
            for k in params.keys():
                param_keys.add(str(k))
        else:
            params = {}

        metrics = _safe_loads(job.metrics)
        for k in metrics.keys():
            metric_keys.add(str(k))

        v, mkey = _trial_value(metrics, target_metric)
        if v is not None:
            if best_value is None or _is_better(v, best_value, direction):
                best_value = v
                best_metric = mkey

        status = (job.status or "").lower()
        if status == "done":
            n_done += 1
        elif status == "failed":
            n_failed += 1

        # ``trial_id`` may arrive as int or string — coerce so the
        # FE table sorts numerically; bad values fall back to 0
        # rather than blocking the page.
        try:
            trial_id_int = int(opt["trial_id"])
        except (TypeError, ValueError):
            trial_id_int = 0

        rows.append(
            TrialRow(
                trial_id=trial_id_int,
                job_id=job.id,
                batch_id=job.batch_id,
                status=job.status,
                start_time=job.start_time,
                end_time=job.end_time,
                elapsed_s=job.elapsed_s,
                params=params,
                value=v,
                metric_name=mkey,
                metrics=metrics or None,
            )
        )

    if not rows:
        # Empty study (or one the caller cannot see) — same shape, zero
        # rows. We deliberately don't 404 because a study running its
        # very first trial may still be ``running`` with no metrics.
        return StudyDetailOut(
            study_name=study_name,
            direction=direction,
            sampler=sampler,
            n_trials=0,
            n_done=0,
            n_failed=0,
            best_value=None,
            best_metric=None,
            param_keys=[],
            metric_keys=[],
            trials=[],
        )

    # Sorting. ``value`` sort respects direction so ``asc`` always means
    # "best at the top" — minimize → ascending, maximize → descending.
    reverse = order == "desc"
    if sort == "value":
        if order == "asc" and direction and direction.lower() == "maximize":
            reverse = True
        rows.sort(
            key=lambda r: (r.value is None, r.value if r.value is not None else 0.0),
            reverse=reverse,
        )
    elif sort == "trial_id":
        rows.sort(key=lambda r: r.trial_id, reverse=reverse)
    else:  # start_time
        rows.sort(
            key=lambda r: (r.start_time is None, r.start_time or ""),
            reverse=reverse,
        )

    return StudyDetailOut(
        study_name=study_name,
        direction=direction,
        sampler=sampler,
        n_trials=len(rows),
        n_done=n_done,
        n_failed=n_failed,
        best_value=best_value,
        best_metric=best_metric,
        param_keys=sorted(param_keys),
        metric_keys=sorted(metric_keys),
        trials=rows,
    )


# ---------------------------------------------------------------------------
# GET /api/studies/{name}/trials/{trial_id}
# ---------------------------------------------------------------------------
@router.get(
    "/{study_name}/trials/{trial_id}",
    response_model=TrialDetailOut,
)
async def get_trial(
    study_name: str,
    trial_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> TrialDetailOut:
    """Return a single trial — used by the FE drill-down link.

    404 if either the study or the trial isn't visible to the caller.
    The job_id + batch_id are exposed so the FE can deep-link to
    ``/batches/{batch}/jobs/{job}`` for the full timeline.
    """
    pairs = await _visible_jobs(user, db)
    target_metric: str | None = None

    for job, _batch in pairs:
        extra = _safe_loads(job.extra)
        opt = _extract_optuna(extra)
        if opt is None or str(opt["study_name"]) != study_name:
            continue

        try:
            this_trial = int(opt["trial_id"])
        except (TypeError, ValueError):
            continue
        if this_trial != trial_id:
            continue

        if target_metric is None and opt.get("target_metric"):
            target_metric = opt.get("target_metric")

        params = opt.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        metrics = _safe_loads(job.metrics) or None
        v, mkey = _trial_value(metrics or {}, target_metric)

        return TrialDetailOut(
            study_name=study_name,
            trial_id=trial_id,
            job_id=job.id,
            batch_id=job.batch_id,
            status=job.status,
            start_time=job.start_time,
            end_time=job.end_time,
            elapsed_s=job.elapsed_s,
            params=params,
            metrics=metrics,
            value=v,
            metric_name=mkey,
        )

    raise HTTPException(status_code=404, detail=tr(locale, "study.trial_not_found"))
