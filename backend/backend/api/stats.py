"""Aggregate statistics endpoints (roadmap #11 — per-user GPU-hours tile).

Currently exposes one route:

``GET /api/stats/gpu-hours-by-user?days=30``
    Returns ``[{user_id, username, gpu_hours, job_count}]`` aggregating
    ``Job.elapsed_s * (gpu_count or 1) / 3600`` grouped by
    ``Batch.owner_id`` over the last ``days`` days (default 30, capped
    at 365).

Visibility rules:
    * Admin → one row per user who has any qualifying job in the window
    * Non-admin → exactly one row (themselves), even if they have zero
      jobs (gpu_hours=0, job_count=0) so the frontend has a stable shape.

The per-job ``gpu_count`` is parsed from ``Job.metrics`` JSON when the
reporter surfaced it there (keys tried: ``gpu_count``, ``GPU_Count``,
``n_gpus``). Missing → treated as 1, matching the single-GPU default of
the current reporter.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_session
from backend.deps import get_current_user
from backend.models import Batch, Job, User
from backend.utils.response_cache import default_cache as _response_cache

router = APIRouter(prefix="/api/stats", tags=["stats"])


class GpuHoursRow(BaseModel):
    """One row of the GPU-hours leaderboard tile."""

    model_config = ConfigDict(extra="forbid")

    user_id: int
    username: str
    gpu_hours: float
    job_count: int


def _extract_gpu_count(metrics_json: str | None) -> int:
    """Parse ``gpu_count`` (or aliases) out of the stored metrics JSON.

    Returns 1 when the field is absent or the JSON is malformed — the
    single-GPU default matches every reporter run before the phase-4
    multi-GPU wiring.
    """
    if not metrics_json:
        return 1
    try:
        parsed = json.loads(metrics_json)
    except (json.JSONDecodeError, TypeError):
        return 1
    if not isinstance(parsed, dict):
        return 1
    for key in ("gpu_count", "GPU_Count", "n_gpus"):
        v = parsed.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 1


def _window_start_iso(days: int) -> str:
    """ISO-8601 UTC timestamp of ``now - days``."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return start.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@router.get("/gpu-hours-by-user", response_model=list[GpuHoursRow])
async def gpu_hours_by_user(
    days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Lookback window in days (1..365, default 30).",
    ),
    current: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[GpuHoursRow]:
    """Per-user GPU-hour totals over the last ``days`` days."""
    # Per-user key — admin sees every user, non-admin sees just themselves.
    # Include ``days`` so the same tile with a different lookback doesn't
    # collide.
    key = f"gpu-hours:u{current.id}:{days}"

    async def _load() -> list[GpuHoursRow]:
        window_start = _window_start_iso(days)

        # Join Job → Batch → User and filter to:
        #   * jobs with non-null elapsed_s (otherwise no contribution)
        #   * batches with an owner (anonymous batches would be a zero-effect
        #     group anyway)
        #   * jobs ended within the window (fall back to start_time when
        #     end_time is absent — a job still running today still counts)
        stmt = (
            select(
                User.id,
                User.username,
                Job.elapsed_s,
                Job.metrics,
            )
            .join(Batch, Batch.id == Job.batch_id)
            .join(User, User.id == Batch.owner_id)
            .where(Job.elapsed_s.is_not(None))
            .where(
                (Job.end_time.is_not(None) & (Job.end_time >= window_start))
                | (Job.end_time.is_(None) & (Job.start_time >= window_start))
            )
        )
        if not current.is_admin:
            stmt = stmt.where(User.id == current.id)

        rows = (await session.execute(stmt)).all()

        # Aggregate in Python — SQLite has no reliable JSON-field arithmetic
        # and the row count (even across a 365-day window) is small enough
        # that an in-process reduce dominates by wall-clock.
        buckets: dict[int, dict[str, Any]] = {}
        for user_id, username, elapsed_s, metrics_json in rows:
            gpu_count = _extract_gpu_count(metrics_json)
            gpu_seconds = float(elapsed_s or 0) * gpu_count
            slot = buckets.setdefault(
                user_id,
                {"user_id": user_id, "username": username,
                 "gpu_seconds": 0.0, "job_count": 0},
            )
            slot["gpu_seconds"] += gpu_seconds
            slot["job_count"] += 1

        # Non-admin always returns exactly one row, even if empty — keeps
        # the frontend tile layout stable.
        if not current.is_admin and current.id not in buckets:
            buckets[current.id] = {
                "user_id": current.id,
                "username": current.username,
                "gpu_seconds": 0.0,
                "job_count": 0,
            }

        results = [
            GpuHoursRow(
                user_id=row["user_id"],
                username=row["username"],
                gpu_hours=round(row["gpu_seconds"] / 3600.0, 4),
                job_count=row["job_count"],
            )
            for row in buckets.values()
        ]
        # Stable order: highest GPU-hours first, then username ascending.
        results.sort(key=lambda r: (-r.gpu_hours, r.username))
        return results

    return await _response_cache.get_or_compute(key, _load)
