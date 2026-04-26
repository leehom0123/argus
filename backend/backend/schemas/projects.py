"""Pydantic DTOs for the ``/api/projects/*`` endpoints.

Projects are not first-class rows in the DB — they are inferred by
``GROUP BY batch.project``. Every DTO here is assembled on the fly by
:class:`backend.services.dashboard.DashboardService`.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ProjectTopModel(BaseModel):
    """One ``top_models`` row on a project summary (v0.1.3 density pass)."""

    model_config = ConfigDict(extra="forbid")

    model: str | None
    dataset: str | None
    metric_name: str
    metric_value: float


class ProjectSummary(BaseModel):
    """Row in ``GET /api/projects``."""

    model_config = ConfigDict(extra="forbid")

    project: str
    n_batches: int
    running_batches: int
    jobs_done: int
    jobs_failed: int
    last_event_at: str | None
    is_starred: bool
    is_demo: bool = False
    # v0.1.3 density extension. Keep defaults so callers that handcraft
    # a row (e.g. legacy fixtures) don't have to enumerate every field.
    failure_rate: float | None = None
    gpu_hours: float = 0.0
    top_models: list[ProjectTopModel] = []
    batch_volume_7d: list[int] = []


class ProjectDetail(BaseModel):
    """Detail payload for ``GET /api/projects/{project}``.

    ``is_public`` + ``public_description`` are only populated for admin
    callers so the Projects page can render a status chip / open the
    publish modal. Regular authenticated users see ``None`` for both
    (no information leak about which projects are flagged).
    """

    model_config = ConfigDict(extra="forbid")

    project: str
    n_batches: int
    running_batches: int
    jobs_done: int
    jobs_failed: int
    failure_rate: float | None
    gpu_hours: float
    best_metric: dict[str, Any] | None
    first_event_at: str | None
    last_event_at: str | None
    batches_this_week: int = 0
    is_starred: bool
    owners: list[str]
    # Admin-only visibility metadata (None for non-admin callers).
    is_public: bool | None = None
    public_description: str | None = None


class ProjectActiveBatch(BaseModel):
    """One row in ``GET /api/projects/{project}/active-batches``."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    project: str
    owner_id: int | None
    host: str | None
    status: str | None
    n_total: int | None
    n_done: int
    n_failed: int
    completion_pct: float | None
    start_time: str | None
    last_event_at: str | None
    eta_seconds: int | None
    is_stalled: bool
    running_jobs: list[dict[str, Any]]
    warnings: list[str]


class ProjectLeaderboardRow(BaseModel):
    """One row in ``GET /api/projects/{project}/leaderboard``."""

    model_config = ConfigDict(extra="forbid")

    model: str
    dataset: str
    best_metric: float | None
    metric_name: str | None
    batch_id: str | None
    job_id: str | None
    status: str | None
    train_epochs: int | None
    elapsed_s: int | None
    # Full metrics map from the winning job (all keys the reporter sent).
    metrics: dict[str, float] | None


class ProjectMatrixOut(BaseModel):
    """``GET /api/projects/{project}/matrix`` — heatmap data.

    ``rows`` are model names, ``cols`` are dataset names, ``values`` is
    a ``rows × cols`` dense matrix of ``best_metric`` floats (``None``
    where no completed job exists). ``batch_ids`` is a parallel matrix
    where each cell is a list of contributing batch IDs (newest-first,
    up to 3) or ``None`` when the cell has no value. Frontend renders
    this as an ECharts heatmap with batch identity in tooltips.
    """

    model_config = ConfigDict(extra="forbid")

    project: str
    metric: str
    rows: list[str]
    cols: list[str]
    values: list[list[float | None]]
    batch_ids: list[list[list[str] | None]]


class ProjectResourcesOut(BaseModel):
    """``GET /api/projects/{project}/resources`` — GPU-hours etc."""

    model_config = ConfigDict(extra="forbid")

    project: str
    gpu_hours: float
    jobs_completed: int
    avg_job_minutes: float | None
    hourly_heatmap: list[list[int]]  # 7 rows (dow) × 24 cols (hour)
    host_distribution: dict[str, int]


class BatchHealthOut(BaseModel):
    """``GET /api/batches/{id}/health`` — liveness summary."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    is_stalled: bool
    last_event_age_s: int | None
    failure_count: int
    warnings: list[str]
    stalled_threshold_s: int


class BatchEtaOut(BaseModel):
    """``GET /api/batches/{id}/eta`` — EMA-based ETA."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    eta_seconds: int | None
    pending_count: int
    sampled_done_jobs: int
