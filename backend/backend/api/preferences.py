"""``/api/users/me/preferences`` — per-user UI preferences.

Intentionally narrow in scope: settings here are UI-only knobs that
don't change authorization. The endpoint is split out of
``/api/auth/*`` so future preferences (timezone, default metric,
dense/sparse table layout, …) can land without bloating the auth
module.

Two routes:

* ``GET /api/users/me/preferences`` — read
* ``PATCH /api/users/me/preferences`` — update a subset

Both require a real authenticated user (not an API token — only a
human would care about UI chrome). The patch body uses PATCH
semantics: omitted fields leave the stored value unchanged; explicit
``None`` is not accepted (we use ``extra='forbid'`` + type-narrow
schemas so the frontend can't accidentally clear a value by sending
``null``).

.. note::
    ``hide_demo`` is deprecated since 2026-04-24. Demo projects are
    now unconditionally hidden from every authenticated user via
    :class:`backend.services.visibility.VisibilityResolver`, so
    toggling this flag has no effect on what the caller sees. The
    field is retained in the request/response bodies for backwards
    compatibility with older clients that still send it; setting it
    simply round-trips the stored value without changing any
    visibility rule.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_current_user, get_db
from backend.models import User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users/me", tags=["preferences"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PreferencesOut(BaseModel):
    """Response body for GET and PATCH."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    # Deprecated: demo now unconditionally hidden from logged-in users
    # since 2026-04-24. Kept in the schema for backwards compatibility.
    hide_demo: bool = False
    preferred_locale: str = "en-US"


class PreferencesPatchIn(BaseModel):
    """Request body for PATCH.

    All fields optional; only the ones present in the JSON are
    updated. Preferred locale is validated as one of the known codes
    so invalid values don't silently poison later sessions — an
    unknown locale passes through ``tr()`` as the key itself, which
    looks like a bug to users.
    """

    model_config = ConfigDict(extra="forbid")

    # Deprecated: demo now unconditionally hidden from logged-in users
    # since 2026-04-24. The backend still accepts patches for
    # backwards compatibility but the stored flag no longer affects
    # any visibility rule.
    hide_demo: bool | None = Field(default=None)
    preferred_locale: str | None = Field(
        default=None,
        pattern=r"^[a-z]{2}-[A-Z]{2}$",
        description="BCP-47 locale tag; e.g. en-US, zh-CN.",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/preferences", response_model=PreferencesOut)
async def get_preferences(
    user: User = Depends(get_current_user),
) -> PreferencesOut:
    """Return the caller's UI preferences."""
    return PreferencesOut(
        hide_demo=bool(user.hide_demo),
        preferred_locale=user.preferred_locale or "en-US",
    )


@router.patch("/preferences", response_model=PreferencesOut)
async def update_preferences(
    payload: PreferencesPatchIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PreferencesOut:
    """Update a subset of the caller's preferences.

    Each field is applied independently; missing fields keep their
    stored value. A patch with no recognised fields still succeeds —
    it just returns the current values unchanged, which matches
    HTTP PATCH idempotency semantics.
    """
    changed = False
    if payload.hide_demo is not None and payload.hide_demo != user.hide_demo:
        user.hide_demo = payload.hide_demo
        changed = True
    if (
        payload.preferred_locale is not None
        and payload.preferred_locale != user.preferred_locale
    ):
        user.preferred_locale = payload.preferred_locale
        changed = True
    if changed:
        await db.commit()
    return PreferencesOut(
        hide_demo=bool(user.hide_demo),
        preferred_locale=user.preferred_locale or "en-US",
    )
