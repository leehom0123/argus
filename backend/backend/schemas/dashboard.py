"""Pydantic DTOs for the ``/api/dashboard`` aggregation endpoint.

The dashboard endpoint fans out to many internal queries and returns a
single nested payload so the frontend doesn't have to issue N+1 round
trips to paint the home page. Each sub-model mirrors one panel in the
requirements §16.2 layout.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class DashboardCounters(BaseModel):
    """Top "indicator strip" counters (§16.2)."""

    model_config = ConfigDict(extra="forbid")

    running_batches: int
    jobs_running: int
    jobs_done_24h: int
    jobs_failed_24h: int
    active_hosts: int
    avg_gpu_util: float | None
    my_running: int


class DashboardProjectTopModel(BaseModel):
    """One ``top_models`` row on a project card (v0.1.3 density pass)."""

    model_config = ConfigDict(extra="forbid")

    model: str | None
    dataset: str | None
    metric_name: str
    metric_value: float


class DashboardProjectCard(BaseModel):
    """One card in the project grid on the dashboard."""

    model_config = ConfigDict(extra="forbid")

    project: str
    running_batches: int
    jobs_done: int
    jobs_failed: int
    eta_seconds: int | None
    last_event_at: str | None
    is_starred: bool
    is_demo: bool = False
    # v0.1.3 density extension — see services/dashboard.py::_project_cards.
    failure_rate: float | None = None
    gpu_hours: float = 0.0
    top_models: list[DashboardProjectTopModel] = []
    batch_volume_7d: list[int] = []


class DashboardActivityItem(BaseModel):
    """One row of the activity feed (last 20 interesting events)."""

    model_config = ConfigDict(extra="forbid")

    event_type: str
    batch_id: str
    job_id: str | None
    project: str | None
    timestamp: str
    summary: str


class DashboardHostRunningJob(BaseModel):
    """One row of the per-host top-5 running-jobs chip list (v0.1.3)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    model: str | None
    dataset: str | None
    user: str | None
    pid: int | None


class DashboardHostCard(BaseModel):
    """One card in the right-rail host status list."""

    model_config = ConfigDict(extra="forbid")

    host: str
    last_seen: str | None
    gpu_util_pct: float | None
    gpu_mem_mb: float | None
    gpu_mem_total_mb: float | None
    gpu_temp_c: float | None
    cpu_util_pct: float | None
    ram_mb: float | None
    ram_total_mb: float | None
    disk_free_mb: float | None
    disk_total_mb: float | None = None
    running_jobs: int
    running_jobs_top5: list[DashboardHostRunningJob] = []
    warnings: list[str]


class DashboardNotification(BaseModel):
    """One notification row (token expiring, new share, failed run)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    message: str
    timestamp: str
    target_type: str | None = None
    target_id: str | None = None


class DashboardOut(BaseModel):
    """Top-level response for ``GET /api/dashboard``."""

    model_config = ConfigDict(extra="forbid")

    scope: str
    counters: DashboardCounters
    projects: list[DashboardProjectCard]
    activity: list[DashboardActivityItem]
    hosts: list[DashboardHostCard]
    notifications: list[DashboardNotification]
    generated_at: str
