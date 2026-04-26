"""``/api/tokens`` — personal API token self-service.

Three endpoints:

* ``GET /api/tokens`` — list the caller's tokens (no plaintext)
* ``POST /api/tokens`` — mint a fresh token; **only this response
  contains the plaintext**
* ``DELETE /api/tokens/{id}`` — soft-revoke a token (``revoked=True``)

All three require an interactive JWT login (see :func:`require_web_session`).
A reporter token can't mint or revoke other tokens — that would let a
leaked token escalate itself.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.tokens import generate_api_token
from backend.deps import get_db, require_web_session
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import ApiToken, User
from backend.schemas import TokenCreateIn, TokenCreateOut, TokenOut
from backend.services.audit import get_audit_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

# Per-user cap on simultaneously-active (non-revoked) API tokens. Above
# this we return 409 Conflict so the UI can prompt the caller to revoke
# stale tokens before minting more (v0.1.3 hardening).
_ACTIVE_TOKEN_CAP = 50


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@router.get("", response_model=list[TokenOut])
async def list_tokens(
    scope: str | None = Query(
        default=None,
        description=(
            "Optional filter: 'reporter' or 'viewer'. "
            "Defaults to both when omitted."
        ),
    ),
    include_revoked: bool = Query(
        default=False,
        description=(
            "If false (default) revoked tokens are hidden. UI sets true "
            "only on the 'audit' view."
        ),
    ),
    user: User = Depends(require_web_session),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> list[TokenOut]:
    """Return the caller's tokens.

    Ordering is newest-first so the UI's "recently generated" view
    doesn't need to re-sort.
    """
    stmt = select(ApiToken).where(ApiToken.user_id == user.id)
    if not include_revoked:
        stmt = stmt.where(ApiToken.revoked.is_(False))
    if scope is not None:
        if scope not in {"reporter", "viewer"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=tr(locale, "token.scope.invalid"),
            )
        stmt = stmt.where(ApiToken.scope == scope)
    stmt = stmt.order_by(ApiToken.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return [TokenOut.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=TokenCreateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_token(
    payload: TokenCreateIn,
    request: Request,
    user: User = Depends(require_web_session),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> TokenCreateOut:
    """Mint a new API token for the current user.

    This is the only place plaintext ever leaves the server — the DB
    stores only the SHA-256 hash plus an 8-char display hint. Clients
    MUST save the returned ``token`` string at this moment.

    Capped at 50 active (non-revoked) tokens per user (v0.1.3 hardening)
    — returns 409 once the cap is hit so the UI can prompt the caller
    to revoke stale tokens first.
    """
    active_stmt = (
        select(func.count(ApiToken.id))
        .where(ApiToken.user_id == user.id)
        .where(ApiToken.revoked.is_(False))
    )
    active_count = int(
        (await db.execute(active_stmt)).scalar_one() or 0
    )
    if active_count >= _ACTIVE_TOKEN_CAP:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=tr(locale, "token.mint_cap_exceeded"),
        )

    plaintext, token_hash, prefix, display_hint = generate_api_token(
        payload.scope
    )
    row = ApiToken(
        user_id=user.id,
        name=payload.name,
        token_hash=token_hash,
        prefix=prefix,
        display_hint=display_hint,
        scope=payload.scope,
        created_at=_utcnow_iso(),
        last_used=None,
        expires_at=payload.expires_at,
        revoked=False,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info(
        "user id=%d minted api_token id=%d scope=%s",
        user.id,
        row.id,
        row.scope,
    )
    # Sync write so the audit row lands before the response returns.
    # The background variant is available for hotter paths (login) where
    # we accept at-most-once semantics in exchange for latency.
    await get_audit_service().log(
        action="token_create",
        user_id=user.id,
        target_type="api_token",
        target_id=str(row.id),
        metadata={"name": row.name, "scope": row.scope},
        ip=request.client.host if request.client else None,
    )
    return TokenCreateOut(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        display_hint=row.display_hint,
        scope=row.scope,
        created_at=row.created_at,
        last_used=row.last_used,
        expires_at=row.expires_at,
        revoked=row.revoked,
        token=plaintext,
    )


@router.delete("/{token_id}", status_code=status.HTTP_200_OK)
async def revoke_token(
    token_id: int,
    request: Request,
    user: User = Depends(require_web_session),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> dict[str, object]:
    """Soft-revoke (``revoked=True``) the given token.

    Returns 200 on both first-time revoke and repeat requests so the UI
    can retry safely; the response ``detail`` field reports which
    transition happened.
    """
    row = await db.get(ApiToken, token_id)
    if row is None or row.user_id != user.id:
        # Don't distinguish "not yours" from "not found" — same 404
        # either way, so enumeration attacks can't map out token ids.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=tr(locale, "token.not_found")
        )
    already = bool(row.revoked)
    if not already:
        row.revoked = True
        await db.commit()
        log.info(
            "user id=%d revoked api_token id=%d", user.id, row.id
        )
        await get_audit_service().log(
            action="token_revoke",
            user_id=user.id,
            target_type="api_token",
            target_id=str(row.id),
            metadata={"name": row.name, "scope": row.scope},
            ip=request.client.host if request.client else None,
        )
    return {
        "ok": True,
        "detail": "already-revoked" if already else "revoked",
        "id": row.id,
    }
