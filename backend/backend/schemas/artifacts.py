"""Pydantic DTOs for ``/api/jobs/{id}/artifacts`` and ``/api/artifacts/{id}``."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ArtifactOut(BaseModel):
    """One row in the list endpoint.

    ``storage_path`` is deliberately omitted — it's an implementation
    detail and exposing it would leak internal filesystem layout to the
    API. Callers download via ``GET /api/artifacts/{id}``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    batch_id: str
    filename: str
    mime: str
    size_bytes: int
    label: str | None = None
    meta: dict | None = None
    created_at: str
