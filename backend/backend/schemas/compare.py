"""Pydantic DTOs for ``/api/compare`` (batch side-by-side)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


# UX constraint per §16.7 was originally 4 wide for the side-by-side
# column layout. Issue #19 bumps this to 32 so project-wide comparison
# can enumerate a full sweep (e.g. the 32-combination CC ablation) in
# one request. The frontend still renders the first N in columns and
# paginates the rest; 32 is a pragmatic ceiling that keeps response
# bodies under ~1 MB even with 50 jobs per batch.
MAX_COMPARE_BATCHES: int = 32


class CompareJobMetric(BaseModel):
    """One completed job's headline metrics + loss curve."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    model: str | None
    dataset: str | None
    status: str | None
    elapsed_s: int | None
    metrics: dict[str, Any] | None


class CompareBatchColumn(BaseModel):
    """One column of the side-by-side view."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    project: str
    status: str | None
    n_total: int | None
    n_done: int
    n_failed: int
    start_time: str | None
    end_time: str | None
    owner_id: int | None
    jobs: list[CompareJobMetric]
    best_metric: dict[str, Any] | None


class CompareOut(BaseModel):
    """Response for ``GET /api/compare?batches=a,b,c``."""

    model_config = ConfigDict(extra="forbid")

    batches: list[CompareBatchColumn]
    metric_union: list[str]
