"""Pydantic DTOs for ``/api/stars`` (user favourites)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

StarTargetType = Literal["project", "batch"]


class StarIn(BaseModel):
    """Body for ``POST /api/stars``."""

    model_config = ConfigDict(extra="forbid")

    target_type: StarTargetType
    target_id: str = Field(..., min_length=1, max_length=256)


class StarOut(BaseModel):
    """Row in ``GET /api/stars``."""

    model_config = ConfigDict(from_attributes=True)

    target_type: StarTargetType
    target_id: str
    starred_at: str
