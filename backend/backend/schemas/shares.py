"""Pydantic DTOs for the share management endpoints.

Thin HTTP-boundary shapes. Each ``*Out`` model enriches the DB row
with the grantee's username so the frontend can render a list without
a second round-trip per row.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Two-value permission enum — keep in one place so future additions
# (``admin``?) are trivial.
Permission = Literal["viewer", "editor"]


# ---------------------------------------------------------------------------
# Batch-level shares
# ---------------------------------------------------------------------------


class BatchShareIn(BaseModel):
    """Body for ``POST /api/batches/{batch_id}/shares``."""

    model_config = ConfigDict(extra="forbid")

    grantee_username: str = Field(..., min_length=1, max_length=32)
    permission: Permission = "viewer"


class BatchShareOut(BaseModel):
    """One row in the ``GET /api/batches/{batch_id}/shares`` response."""

    model_config = ConfigDict(from_attributes=True)

    batch_id: str
    grantee_id: int
    grantee_username: str
    permission: Permission
    created_at: str
    created_by: int | None = None


# ---------------------------------------------------------------------------
# Project-level shares
# ---------------------------------------------------------------------------


class ProjectShareIn(BaseModel):
    """Body for ``POST /api/projects/shares``."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(..., min_length=1, max_length=256)
    grantee_username: str = Field(..., min_length=1, max_length=32)
    permission: Permission = "viewer"


class ProjectShareOut(BaseModel):
    """One row in the ``GET /api/projects/shares`` response."""

    model_config = ConfigDict(from_attributes=True)

    owner_id: int
    project: str
    grantee_id: int
    grantee_username: str
    permission: Permission
    created_at: str
