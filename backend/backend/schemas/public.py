"""Pydantic DTOs for the public-share endpoints.

The public read surface deliberately omits owner PII. MVP shows the
batch id, project, host, progress counters, and the start / end
timestamps. The owner is exposed only as ``"Shared by user #<id>"``
via the ``owner_label`` field — a future ``public_profile`` toggle
(phase 4) could surface the real username.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PublicShareCreateIn(BaseModel):
    """Body for ``POST /api/batches/{batch_id}/public-share``."""

    model_config = ConfigDict(extra="forbid")

    expires_at: str | None = Field(
        default=None,
        description=(
            "Optional ISO 8601 expiry. After this timestamp, GET /api/public/"
            "{slug} returns 410 Gone. Omit for a never-expiring link."
        ),
    )


class PublicShareOut(BaseModel):
    """Response shape for create / list of public shares."""

    slug: str
    url: str
    batch_id: str
    created_at: str
    expires_at: str | None = None
    view_count: int = 0
    last_viewed: str | None = None


class PublicBatchOut(BaseModel):
    """Anonymised batch projection for ``GET /api/public/{slug}``.

    No ``owner_id`` / ``owner.username`` / ``owner.email`` — only a
    fixed-shape ``owner_label`` like ``"Shared by user #12"``.
    """

    id: str
    project: str
    experiment_type: str | None = None
    host: str | None = None
    command: str | None = None
    n_total: int | None = None
    n_done: int = 0
    n_failed: int = 0
    status: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    owner_label: str


class PublicJobOut(BaseModel):
    """Job row stripped of any owner-leaking fields."""

    id: str
    batch_id: str
    model: str | None = None
    dataset: str | None = None
    status: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    elapsed_s: int | None = None
    metrics: dict[str, Any] | None = None


class PublicEpochsOut(BaseModel):
    """Per-epoch timeseries response."""

    batch_id: str
    job_id: str
    points: list[dict[str, Any]]
