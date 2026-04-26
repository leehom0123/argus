"""Pydantic DTOs for ``/api/pins`` (compare-pool)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# Matches the §16.7 UX constraint (side-by-side view supports up to 4
# columns without overflowing). The compose endpoint enforces this in
# :func:`backend.api.pins.add_pin`; the DB itself does not.
MAX_PINS_PER_USER: int = 4


class PinIn(BaseModel):
    """Body for ``POST /api/pins``."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(..., min_length=1, max_length=128)


class PinOut(BaseModel):
    """Row in ``GET /api/pins`` — includes a minimal batch summary."""

    model_config = ConfigDict(from_attributes=True)

    batch_id: str
    pinned_at: str
    project: str | None = None
    status: str | None = None
    n_total: int | None = None
    n_done: int = 0
    n_failed: int = 0
    start_time: str | None = None
