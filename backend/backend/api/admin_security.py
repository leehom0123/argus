"""``/api/admin/security/*`` — JWT secret rotation surface (v0.2 #109).

Today this router exposes a single action — ``POST /jwt/rotate`` — but
it sits on its own prefix so future security knobs (audit-only IP
allowlists, MFA enforcement toggles, …) land here without churning
``admin_config``'s catalogue.

Design notes
------------
* All routes gated on :func:`backend.deps.require_admin`.
* The rotation flow is intentionally minimal: mint, demote, write,
  return. We do **not** invalidate active sessions — the dual-key
  decoder accepts both secrets for the 24h grace window, so users stay
  logged in across a rotation.
* The legacy ``ARGUS_JWT_SECRET`` env var still wins for *signing* until
  the first rotation, because the new ``current_secret`` row is empty
  on a fresh install. Rotating once flips the deployment over to the
  DB-backed flow permanently — there is no UI to "go back to env
  only", which is the safe default.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_db, require_admin
from backend.models import User
from backend.services.audit import get_audit_service
from backend.services.jwt_rotation import (
    PREVIOUS_GRACE_SECONDS,
    RotationCooldown,
    load_secrets,
    rotate_secret,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/security", tags=["admin"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class JwtStatusOut(BaseModel):
    """Response shape for ``GET /jwt/status``.

    Drives the Settings → Admin → Security panel: we surface the last
    rotation time and whether a previous secret is still in the grace
    window so the UI can render the countdown without exposing any
    secret material.
    """

    rotated_at: str | None = Field(
        default=None,
        description="UTC ISO timestamp of the last rotation; null when never rotated.",
    )
    has_previous: bool = Field(
        default=False,
        description="True iff a previous_secret row is currently honoured by the verifier.",
    )
    previous_expires_at: str | None = Field(
        default=None,
        description="UTC ISO timestamp at which the previous secret stops being accepted.",
    )
    grace_seconds: int = Field(
        default=PREVIOUS_GRACE_SECONDS,
        description="Length of the rolling grace window in seconds.",
    )


class JwtRotateOut(BaseModel):
    """Response shape for ``POST /jwt/rotate``."""

    rotated_at: str = Field(..., description="UTC ISO timestamp written for this rotation.")
    grace_seconds: int = Field(
        default=PREVIOUS_GRACE_SECONDS,
        description=(
            "Number of seconds the prior secret remains valid for token "
            "verification (so existing logins are not severed)."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/jwt/status", response_model=JwtStatusOut)
async def jwt_status(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JwtStatusOut:
    """Read-only view of the rotation state.

    Never exposes any secret material — the admin UI uses this to
    render the "Last rotated: …" label and the "Previous secret
    expires in …" countdown.
    """
    _, previous, rotated_at = await load_secrets(db)

    previous_expires_at: str | None = None
    has_previous = bool(previous)
    if has_previous and rotated_at:
        # ``rotated_at`` is when ``previous`` was created. Adding the
        # grace gives the absolute expiry — the UI does the live
        # countdown client-side using this fixed value.
        from datetime import datetime, timedelta, timezone
        try:
            iso = rotated_at[:-1] + "+00:00" if rotated_at.endswith("Z") else rotated_at
            base = datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            base = None
        if base is not None:
            expires = base + timedelta(seconds=PREVIOUS_GRACE_SECONDS)
            previous_expires_at = (
                expires.astimezone(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )

    return JwtStatusOut(
        rotated_at=rotated_at,
        has_previous=has_previous,
        previous_expires_at=previous_expires_at,
    )


@router.post("/jwt/rotate", response_model=JwtRotateOut)
async def jwt_rotate(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JwtRotateOut:
    """Mint a new signing secret and demote the current one.

    The previous secret continues to verify already-issued tokens for
    :data:`backend.services.jwt_rotation.PREVIOUS_GRACE_SECONDS` seconds
    so nobody is force-logged-out by the rotation. After the grace
    window the background sweeper clears the row.

    A 60-second cooldown is enforced server-side to prevent a curl loop
    or accidental double-click from rotating twice in quick succession —
    the second rotation would overwrite the freshly-minted previous
    secret with a blank, invalidating every JWT issued before the first
    rotation. Returns 429 with ``Retry-After`` when the cooldown bites.
    """
    try:
        rotated_at = await rotate_secret(db, actor_user_id=admin.id)
    except RotationCooldown as cd:
        log.info(
            "jwt_rotation: cooldown rejected rotate by admin %s (%s); retry_after=%ss",
            admin.id, admin.username, cd.retry_after,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"retry_after": cd.retry_after},
            headers={"Retry-After": str(cd.retry_after)},
        ) from cd
    await get_audit_service().log(
        action="jwt_rotate",
        user_id=admin.id,
        target_type="system_config",
        target_id="jwt/current_secret",
        metadata={"rotated_at": rotated_at},
        ip=_client_ip(request),
    )
    log.info(
        "jwt_rotation: admin %s (%s) rotated JWT secret at %s",
        admin.id, admin.username, rotated_at,
    )
    return JwtRotateOut(rotated_at=rotated_at)


__all__ = ["router"]
