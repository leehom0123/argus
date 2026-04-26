"""Pydantic DTOs for the admin-controlled **public demo** surface.

Two audiences share this module:

* Admins POST :class:`PublicProjectPublishIn` / read
  :class:`PublicProjectMetaOut` when toggling visibility.
* Anonymous visitors read :class:`PublicProjectSummary` /
  :class:`PublicProjectDetail` (no PII, smaller than the admin / owner
  versions).

Kept separate from :mod:`backend.schemas.projects` so the anonymous
surface never accidentally grows owner-leaking fields.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PublicProjectPublishIn(BaseModel):
    """Request body for ``POST /api/admin/projects/{project}/publish``."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Short description shown on the /demo landing page. "
            "Max 500 chars. Omit to keep the previous value (or None)."
        ),
    )


class PublicProjectMetaOut(BaseModel):
    """Response shape for publish / unpublish / admin list endpoints."""

    model_config = ConfigDict(extra="forbid")

    project: str
    is_public: bool
    public_description: str | None = None
    published_at: str | None = None
    published_by_user_id: int | None = None


class PublicProjectSummary(BaseModel):
    """Row in ``GET /api/public/projects`` — anonymous list view."""

    model_config = ConfigDict(extra="forbid")

    project: str
    description: str | None = None
    published_at: str | None = None
    n_batches: int


class PublicProjectDetail(BaseModel):
    """Header payload for ``GET /api/public/projects/{project}``.

    Strict superset of the aggregated stats a public visitor sees —
    still no owner identity beyond batch / job counters. Keeps the
    same stat keys as :class:`ProjectDetail` so the frontend can reuse
    its existing project-detail layout without conditionals.
    """

    model_config = ConfigDict(extra="forbid")

    project: str
    description: str | None = None
    published_at: str | None = None
    n_batches: int
    running_batches: int
    jobs_done: int
    jobs_failed: int
    failure_rate: float | None = None
    gpu_hours: float
    first_event_at: str | None = None
    last_event_at: str | None = None


class PublicProjectBatch(BaseModel):
    """Batch row in ``GET /api/public/projects/{project}/batches``.

    Metadata only — no owner_id, no raw command, no job-level logs.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    project: str
    host: str | None = None
    status: str | None = None
    n_total: int | None = None
    n_done: int = 0
    n_failed: int = 0
    start_time: str | None = None
    end_time: str | None = None
