"""Pydantic DTOs for the /api/tokens endpoints.

Three shapes live side by side:

* :class:`TokenCreateIn`  — POST body
* :class:`TokenCreateOut` — POST response (the *only* place the plaintext
  token is ever returned)
* :class:`TokenOut`       — list / detail response; no plaintext, only
  ``display_hint``

Keeping the Create-vs-Read split explicit makes it impossible to
accidentally leak the plaintext through a list endpoint.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Scope = Literal["reporter", "viewer"]


class TokenCreateIn(BaseModel):
    """Body for ``POST /api/tokens``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="User-provided label, e.g. 'laptop-reporter'.",
    )
    scope: Scope = Field(
        ...,
        description=(
            "'reporter' — em_live_ prefix, ingest + read; "
            "'viewer' — em_view_ prefix, read-only."
        ),
    )
    expires_at: str | None = Field(
        default=None,
        description=(
            "Optional ISO 8601 timestamp. If omitted, the token never "
            "expires (only explicit revoke can disable it)."
        ),
    )

    @field_validator("expires_at")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class TokenOut(BaseModel):
    """Projection for ``GET /api/tokens`` — no plaintext ever."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    prefix: str
    display_hint: str
    scope: Scope
    created_at: str
    last_used: str | None = None
    expires_at: str | None = None
    revoked: bool = False


class TokenCreateOut(TokenOut):
    """Response for ``POST /api/tokens``.

    Inherits the read-side projection and tacks on the plaintext
    ``token``. This is the one and only time the plaintext leaves the
    server; clients must copy it to secure storage immediately.
    """

    token: str = Field(
        ...,
        description=(
            "Plaintext API token (em_live_… / em_view_…). Shown once; "
            "store it now — the server keeps only the SHA-256 hash."
        ),
    )
