"""Pydantic DTOs for the ``/api/admin/*`` endpoints."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AdminUserOut(BaseModel):
    """User projection for the admin listing.

    Unlike :class:`backend.schemas.auth.UserOut` this also exposes the
    lock state + failed-login counter so admins can triage.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    is_active: bool
    is_admin: bool
    email_verified: bool
    created_at: str
    last_login: str | None = None
    failed_login_count: int = 0
    locked_until: str | None = None


class FeatureFlagOut(BaseModel):
    """One ``feature_flag`` row, value JSON-decoded to a native type."""

    key: str
    value: Any
    updated_at: str | None = None
    updated_by: int | None = None


class FeatureFlagUpdateIn(BaseModel):
    """Body for ``PUT /api/admin/feature-flags/{key}``.

    ``value`` is a free-form JSON-compatible payload so a single
    endpoint can flip bools, set ints, and update small dicts.
    """

    model_config = ConfigDict(extra="forbid")

    value: Any = Field(..., description="New value (any JSON-serialisable shape)")
