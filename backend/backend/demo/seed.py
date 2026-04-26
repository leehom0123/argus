"""Seed a polished, deterministic demo project into the database.

Rationale
---------

Showing real user data on the public landing page is risky — command
strings may contain server paths, log lines may contain emails, and a
batch that is half-failing does not sell the product. The demo
fixture gives the UI something **guaranteed to be glamorous and
risk-free**: 60 jobs (48 done with realistic forecasting metrics, 10
running, 2 failed), ~100 per-host resource snapshots that ramp to a
plausible shape, and 30 generic log lines.

Entry points
------------

* :func:`seed_demo` — idempotent; call on startup. Noop when the demo
  project row already exists. ``force=True`` wipes and regenerates so
  admins can refresh the fixture after a schema change.
* :data:`DEMO_PROJECT` — ``"__demo_forecast__"``. The double-underscore
  prefix prevents collision with real user-chosen project names.

Determinism
-----------

All randomness goes through a single ``random.Random(seed=42)``
instance; timestamps are computed relative to ``_utcnow()`` at seed
time so every call produces a visually-coherent "started 6h ago"
timeline without persisting obsolete absolute values. Metric values
live inside plausible forecasting ranges (MSE 0.1–1.2, R² 0.4–0.8,
RMSE 0.3–1.2, ...) so the leaderboard feels like a real benchmark.

Memory + time budget
--------------------

On the in-memory SQLite used by tests the seeder inserts ~200 rows
and returns in well under 100 ms. Production SQLite takes a single
transaction round-trip.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Batch, Event, Job, ProjectMeta, ResourceSnapshot

log = logging.getLogger(__name__)


# Public identifiers — exported at package level so tests and callers
# can refer to them without duplicating the string literal.
DEMO_PROJECT = "__demo_forecast__"
DEMO_BATCH_ID = "demo-bench-001"
DEMO_HOST = "demo-host-a100"
DEMO_DESCRIPTION = (
    "Demo pipeline — 12 forecasting models on 5 datasets"
)

_MODELS = (
    "transformer",
    "informer",
    "autoformer",
    "fedformer",
    "dlinear",
    "patchtst",
    "timesnet",
    "itransformer",
    "timemixer",
    "timexer",
    "softs",
    "timefilter",
)
_DATASETS = ("etth1", "etth2", "electricity", "traffic", "weather")

# 3 concurrent pids that back the 10 "running" jobs and show up in
# proc_* snapshot columns. The pids are arbitrary but stable across
# seeds so a screenshot stays reproducible.
_RUNNING_PIDS = (101_001, 101_002, 101_003)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Deletion helper (used by force=True and the DELETE end of the round-trip)
# ---------------------------------------------------------------------------


async def _wipe_demo(db: AsyncSession) -> None:
    """Delete every row this module previously inserted.

    Covers ProjectMeta, every Batch whose id starts with ``demo-``,
    their Jobs and Events, plus ResourceSnapshot rows for the demo
    host. We do not ``cascade`` because no cascades are configured on
    the FK columns — explicit deletes keep the behaviour predictable
    across SQLite / Postgres.
    """
    # Find demo batches.
    batch_ids = (
        await db.execute(
            select(Batch.id).where(Batch.id.like("demo-%"))
        )
    ).scalars().all()

    if batch_ids:
        await db.execute(
            delete(Event).where(Event.batch_id.in_(batch_ids))
        )
        await db.execute(delete(Job).where(Job.batch_id.in_(batch_ids)))
        await db.execute(
            delete(ResourceSnapshot).where(
                ResourceSnapshot.batch_id.in_(batch_ids)
            )
        )
        await db.execute(delete(Batch).where(Batch.id.in_(batch_ids)))

    # Resource snapshots keyed by host (some rows might not carry
    # batch_id — e.g. idle-host samples).
    await db.execute(
        delete(ResourceSnapshot).where(ResourceSnapshot.host == DEMO_HOST)
    )

    await db.execute(
        delete(ProjectMeta).where(ProjectMeta.project == DEMO_PROJECT)
    )


# ---------------------------------------------------------------------------
# Builders — each returns the ORM rows to ``db.add_all``.
# ---------------------------------------------------------------------------


def _metric_row(rng: random.Random, i: int) -> dict[str, float]:
    """Return a plausible forecast-metric dict for a done job.

    Values are anchored at realistic TimeXer-ish ranges and jittered by
    the job index so rows look distinct on the leaderboard without any
    single metric dominating. Every key is a float so the leaderboard
    serialiser (``_safe_metrics``) round-trips them untouched.
    """
    base_mse = 0.15 + rng.random() * 0.9          # 0.15 – 1.05
    mse = round(base_mse + rng.random() * 0.15, 4)
    mae = round(mse * (0.6 + rng.random() * 0.25), 4)
    rmse = round(mse ** 0.5, 4)
    r2 = round(0.4 + rng.random() * 0.4, 4)       # 0.4 – 0.8
    pcc = round(min(0.99, r2 + 0.05 + rng.random() * 0.1), 4)
    mape = round(0.08 + rng.random() * 0.12, 4)   # 0.08 – 0.20
    smape = round(mape * (0.9 + rng.random() * 0.2), 4)
    mase = round(0.7 + rng.random() * 0.5, 4)
    rae = round(0.5 + rng.random() * 0.35, 4)
    epochs = 30 + (i % 21)                         # 30 – 50
    elapsed = int(720 + rng.random() * 1800)       # 12–42 min
    return {
        "MSE": mse,
        "MAE": mae,
        "RMSE": rmse,
        "R2": r2,
        "PCC": pcc,
        "MAPE": mape,
        "sMAPE": smape,
        "MASE": mase,
        "RAE": rae,
        "train_epochs": epochs,
        "elapsed_s": elapsed,
        # Callback-style metadata that the leaderboard uses for display.
        "GPU_Memory": round(3500 + rng.random() * 3500, 0),
        "Total_Train_Time": elapsed,
    }


def _build_batch(now: datetime) -> Batch:
    """One ``running`` batch shown on the demo landing."""
    return Batch(
        id=DEMO_BATCH_ID,
        experiment_type="forecast",
        project=DEMO_PROJECT,
        user="demo",
        host=DEMO_HOST,
        # Intentionally generic — no absolute paths, no secrets.
        command="python scripts/forecast/run_benchmark.py --epochs 50",
        n_total=60,
        n_done=48,
        n_failed=2,
        status="running",
        start_time=_iso(now - timedelta(hours=6)),
        end_time=None,
        extra=json.dumps({"demo": True}),
        owner_id=None,                      # not owned by any real user
        is_deleted=False,
        name="Benchmark · 12 models × 5 datasets",
        tag="demo",
    )


def _build_jobs(rng: random.Random, now: datetime) -> list[Job]:
    """60 jobs covering 12 models × 5 datasets.

    48 done (first in list so the leaderboard has plenty of data),
    10 running (the 3 concurrent pids plus 7 queued/starting), and
    2 failed (one timeout, one OOM). Done rows carry the metric bag
    above; failed rows carry an error snippet (no PII). Running rows
    leave ``metrics`` as None so the Active tab renders the right
    "waiting for first epoch" affordance.
    """
    jobs: list[Job] = []
    combos = [(m, d) for m in _MODELS for d in _DATASETS][:60]
    assert len(combos) == 60

    # Index 0..47 → done, 48..57 → running, 58..59 → failed.
    for i, (model, dataset) in enumerate(combos):
        job_id = f"{model}_{dataset}"
        start = now - timedelta(hours=6) + timedelta(minutes=i * 6)
        if i < 48:
            metrics = _metric_row(rng, i)
            end = start + timedelta(seconds=metrics["elapsed_s"])
            jobs.append(Job(
                id=job_id,
                batch_id=DEMO_BATCH_ID,
                model=model,
                dataset=dataset,
                status="done",
                start_time=_iso(start),
                end_time=_iso(end),
                elapsed_s=int(metrics["elapsed_s"]),
                metrics=json.dumps(metrics),
                extra=json.dumps({"pid": rng.choice(_RUNNING_PIDS)}),
            ))
        elif i < 58:
            jobs.append(Job(
                id=job_id,
                batch_id=DEMO_BATCH_ID,
                model=model,
                dataset=dataset,
                status="running",
                start_time=_iso(start),
                end_time=None,
                elapsed_s=None,
                metrics=None,
                extra=json.dumps({
                    "pid": _RUNNING_PIDS[i % len(_RUNNING_PIDS)],
                }),
            ))
        else:
            reason, msg = (
                ("timeout", "wall-clock limit exceeded after 7200 s")
                if i == 58
                else ("oom", "CUDA out of memory: tried to allocate 3.24 GiB")
            )
            jobs.append(Job(
                id=job_id,
                batch_id=DEMO_BATCH_ID,
                model=model,
                dataset=dataset,
                status="failed",
                start_time=_iso(start),
                end_time=_iso(start + timedelta(minutes=45)),
                elapsed_s=45 * 60,
                metrics=json.dumps({"error_reason": 1.0}),
                extra=json.dumps({"reason": reason, "error": msg}),
            ))
    return jobs


def _build_snapshots(
    rng: random.Random, now: datetime
) -> list[ResourceSnapshot]:
    """100 host snapshots on a smooth 0→85% GPU ramp with per-proc split.

    Every sample also emits proc_* fields whose sum is strictly less
    than the host totals, so the "batch share of host" chart in
    StackedResourceChart has a clean envelope.
    """
    snaps: list[ResourceSnapshot] = []
    total_gpu_mem = 24_000.0
    total_ram_mb = 64_000.0
    start_ts = now - timedelta(hours=6)
    # 100 samples across 50 minutes (30-second stride).
    for i in range(100):
        t = start_ts + timedelta(seconds=i * 30)
        ramp = min(0.85, i / 95.0)
        gpu_util = round(ramp * 85 + rng.uniform(-2.5, 2.5), 2)
        gpu_util = max(0.0, min(100.0, gpu_util))
        gpu_mem = round(1000 + ramp * 6000 + rng.uniform(-200, 200), 0)
        cpu_util = round(5 + rng.uniform(0, 35), 2)
        ram_mb = round(4000 + rng.uniform(0, 4000), 0)
        disk_free = round(480_000 + rng.uniform(-2000, 2000), 0)  # ~480 GB
        # Per-process split across the three demo pids — each one sees
        # a third of the GPU + a share of CPU/RAM. All three sum to
        # less than host to keep the stacked chart below capacity.
        pid = _RUNNING_PIDS[i % len(_RUNNING_PIDS)]
        proc_gpu_mem = max(0, int(gpu_mem / 3.5 + rng.uniform(-80, 80)))
        proc_cpu = round(max(0.0, cpu_util / 3.2 + rng.uniform(-1.0, 1.0)), 2)
        proc_ram = max(0, int(ram_mb / 3.5 + rng.uniform(-100, 100)))
        snaps.append(ResourceSnapshot(
            host=DEMO_HOST,
            timestamp=_iso(t),
            gpu_util_pct=gpu_util,
            gpu_mem_mb=gpu_mem,
            gpu_mem_total_mb=total_gpu_mem,
            gpu_temp_c=round(45 + ramp * 25 + rng.uniform(-2, 2), 1),
            cpu_util_pct=cpu_util,
            ram_mb=ram_mb,
            ram_total_mb=total_ram_mb,
            disk_free_mb=disk_free,
            proc_cpu_pct=proc_cpu,
            proc_ram_mb=proc_ram,
            proc_gpu_mem_mb=proc_gpu_mem,
            batch_id=DEMO_BATCH_ID,
            extra=json.dumps({"pid": pid, "demo": True}),
        ))
    return snaps


def _build_events(rng: random.Random, now: datetime) -> list[Event]:
    """30 generic log lines across info / warning / error levels.

    Content is deliberately vague — no file paths, no user names. We
    use log-line events (matching the schema the batch live-panel
    reads) so the UI renders them in the right widget.
    """
    samples = [
        ("info", "Epoch 12/50 val_loss=0.342 train_loss=0.317"),
        ("info", "Epoch 13/50 val_loss=0.328 train_loss=0.305"),
        ("info", "Epoch 14/50 val_loss=0.319 train_loss=0.298"),
        ("info", "Saved best checkpoint (val_loss=0.302)"),
        ("warning", "Learning rate reduced to 5e-5 (plateau)"),
        ("info", "Validation PCC=0.812, SCC=0.805"),
        ("info", "Early stopping patience 3/10"),
        ("info", "GPU memory usage stable at 6.4 GiB"),
        ("warning", "Gradient norm 4.21 (clipped)"),
        ("info", "Epoch 25/50 val_loss=0.287 train_loss=0.269"),
        ("error", "Job timeout: wall-clock limit exceeded"),
        ("info", "Retrying from last checkpoint"),
        ("info", "Using mixed precision (AMP) for this run"),
        ("info", "Epoch 30/50 val_loss=0.275 train_loss=0.258"),
        ("warning", "Disk free space below 20 GiB"),
        ("info", "Batch leaderboard updated (12 new rows)"),
        ("info", "Epoch 40/50 val_loss=0.268 train_loss=0.251"),
        ("error", "CUDA out of memory: tried to allocate 3.24 GiB"),
        ("info", "Job completed successfully"),
        ("info", "Posting metrics to Argus"),
        ("info", "Epoch 45/50 val_loss=0.264 train_loss=0.245"),
        ("info", "Inference on test set: PCC=0.821"),
        ("info", "Epoch 50/50 val_loss=0.261 train_loss=0.242"),
        ("info", "Best MSE 0.182 across 48 done jobs"),
        ("info", "Worst MSE 0.973 (outlier flagged)"),
        ("warning", "Host GPU temperature 82C"),
        ("info", "Running job count: 3"),
        ("info", "Scheduled 10 more jobs"),
        ("info", "Batch progress 48/60 (80%)"),
        ("info", "ETA 1h 22m"),
    ]
    out: list[Event] = []
    base = now - timedelta(hours=6)
    for i, (level, text) in enumerate(samples):
        out.append(Event(
            batch_id=DEMO_BATCH_ID,
            job_id=None,
            event_type="log_line",
            timestamp=_iso(base + timedelta(minutes=i * 12)),
            schema_version="1.1",
            data=json.dumps({"level": level, "message": text}),
            event_id=f"demo-log-{i:03d}",
        ))
    return out


def _build_project_meta(now: datetime, existing: ProjectMeta | None) -> ProjectMeta:
    """Upsert the :class:`ProjectMeta` row.

    Kept as a builder (not an inline ``db.add``) so the force-reset
    path and the first-seed path share the same field values.
    """
    payload = {
        "project": DEMO_PROJECT,
        "is_public": True,
        "public_description": DEMO_DESCRIPTION,
        "published_at": _iso(now),
        "published_by_user_id": None,
        "is_demo": True,
    }
    if existing is None:
        return ProjectMeta(**payload)
    for key, value in payload.items():
        setattr(existing, key, value)
    return existing


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def seed_demo(db: AsyncSession, force: bool = False) -> bool:
    """Ensure the ``__demo_forecast__`` fixture exists.

    Returns ``True`` if the demo was freshly seeded (or re-seeded via
    ``force=True``), ``False`` if an existing demo project was found
    and ``force=False`` (the call was a no-op).

    The function opens no transactions of its own — it relies on the
    session's implicit transaction and commits once at the end. Safe
    to call from ``lifespan`` even on a warm database because the
    ``SELECT 1 FROM project_meta WHERE project=...`` lookup is cheap.
    """
    existing = await db.get(ProjectMeta, DEMO_PROJECT)
    if existing is not None and not force:
        log.debug("seed_demo: %s already present, skipping", DEMO_PROJECT)
        return False

    if force:
        await _wipe_demo(db)
        existing = None

    now = _utcnow()
    rng = random.Random(42)  # fixed seed → deterministic fixture

    meta = _build_project_meta(now, existing)
    batch = _build_batch(now)
    jobs = _build_jobs(rng, now)
    snapshots = _build_snapshots(rng, now)
    events = _build_events(rng, now)

    # SQLAlchemy ``add_all`` accepts a flat iterable; ordering matters
    # only for FK parents (Batch must exist before Job). We add in
    # parent→child order to keep that constraint clean on Postgres.
    if existing is None:
        db.add(meta)
    db.add(batch)
    db.add_all(jobs)
    db.add_all(snapshots)
    db.add_all(events)

    await db.commit()
    log.info(
        "seed_demo: wrote %s (1 batch, %d jobs, %d snapshots, %d events)",
        DEMO_PROJECT, len(jobs), len(snapshots), len(events),
    )
    return True


def iter_demo_identifiers() -> Iterable[str]:
    """Helper for external tooling: the stable string keys we seed.

    Exposed so admin scripts can grep for "is this a demo row?" without
    duplicating the constants.
    """
    return (DEMO_PROJECT, DEMO_BATCH_ID, DEMO_HOST)
