"""FastAPI dependency helpers for the auth + data layers.

Kept deliberately small so callers can compose dependencies without
chasing import spaghetti. The token parser understands **two** Bearer
flavours:

* JWT — issued by ``/api/auth/login`` (Web UI).
* Personal API token (``em_live_*`` / ``em_view_*``) — reporter clients.

Scope enforcement lives in :func:`require_reporter_token` and
:func:`require_web_session`; the base :func:`get_current_user` only
cares about authentication, not authorisation.
"""
from __future__ import annotations

import logging
from typing import Annotated, AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.jwt import (
    JWTError,
    decode_token,
    is_blacklisted,
    is_session_revoked,
    touch_session_last_seen,
)
from backend.auth.tokens import lookup_token, touch_last_used
from backend.config import Settings, get_settings
from backend.db import get_session
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import ApiToken, User
from backend.services.email import EmailService, get_email_service
from backend.utils.ratelimit import get_default_bucket

log = logging.getLogger(__name__)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield one :class:`AsyncSession` per request.

    Thin alias over :func:`backend.db.get_session` so routers don't reach
    across package boundaries.
    """
    async for session in get_session():
        yield session


def get_settings_dep() -> Settings:
    """Cacheable :class:`Settings` dependency."""
    return get_settings()


def get_email_service_dep() -> EmailService:
    """EmailService dependency (singleton per process)."""
    return get_email_service()


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------


def _credentials_exc(locale: SupportedLocale = "en-US") -> HTTPException:
    """Build a 401 with locale-aware ``Authentication required`` detail.

    Built per-call (not module-level) so the detail string can pick up
    the caller's ``Accept-Language``. Headers stay identical to the old
    constant so transport-level expectations (``WWW-Authenticate``)
    don't shift.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=tr(locale, "auth.credentials.required"),
        headers={"WWW-Authenticate": "Bearer"},
    )


_API_TOKEN_KEY = "_auth_api_token"
_AUTH_KIND_KEY = "_auth_kind"


async def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> User:
    """Resolve the authenticated user from a Bearer token.

    Two token flavours live side by side (see design §5.2):

    * JWT — issued by ``/api/auth/login`` and consumed everywhere in the
      Web UI.
    * API token (``em_live_*`` / ``em_view_*``) — for reporter clients
      posting events. Resolved via :func:`lookup_token`; rejects
      revoked and expired rows.

    We stash the auth "kind" (``'jwt'`` | ``'api_token'``) and the
    :class:`ApiToken` row on ``request.state`` so downstream scope deps
    can read them without re-parsing the header.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _credentials_exc(locale)

    token = authorization[7:].strip()
    if not token:
        raise _credentials_exc(locale)

    # --- API token branch --------------------------------------------
    if token.startswith("em_live_") or token.startswith("em_view_"):
        row = await lookup_token(db, token)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=tr(locale, "auth.token.api_invalid"),
                headers={"WWW-Authenticate": "Bearer"},
            )
        if row.user is None or not row.user.is_active:
            # User deactivated after token issue — fail closed.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=tr(locale, "auth.token.owner_inactive"),
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Update last_used inline on the request's own session. The spec
        # allows fire-and-forget, but a sync bump adds one small UPDATE
        # and eliminates cross-loop lifetime issues (tasks can get
        # cancelled when the ASGI transport tears down the event loop
        # under the test harness).
        try:
            await touch_last_used(db, row.id)
        except Exception as exc:  # noqa: BLE001
            log.debug("last_used bump failed (non-fatal): %s", exc)
        setattr(request.state, _AUTH_KIND_KEY, "api_token")
        setattr(request.state, _API_TOKEN_KEY, row)
        return row.user

    # --- JWT branch ---------------------------------------------------
    # Refresh the dual-key rotation cache from the request session so
    # this worker picks up rotations performed by another worker within
    # the cache TTL. Best-effort — failures fall back to whatever the
    # cache + env-var fallback already hold, which is the legacy
    # pre-rotation behaviour.
    try:
        from backend.services.jwt_rotation import (  # local import
            hydrate_cache as _jwt_hydrate_cache,
        )
        await _jwt_hydrate_cache(db)
    except Exception as exc:  # noqa: BLE001
        log.debug("jwt rotation hydrate (deps) ignored: %r", exc)

    try:
        payload = decode_token(token)
    except JWTError as exc:
        log.debug("JWT decode failed: %s", exc)
        raise _credentials_exc(locale) from exc

    if await is_blacklisted(token):
        raise _credentials_exc(locale)

    # DB-backed revocation: Settings > Sessions flips ``revoked_at`` on
    # the ``active_sessions`` row. Check it alongside the in-memory
    # blacklist so a revoke survives a process restart.
    jti = payload.get("jti")
    if jti and await is_session_revoked(db, str(jti)):
        raise _credentials_exc(locale)

    user = await db.get(User, int(payload["user_id"]))
    if user is None or not user.is_active:
        raise _credentials_exc(locale)
    setattr(request.state, _AUTH_KIND_KEY, "jwt")
    # Stash the jti so downstream handlers (GET /api/auth/sessions)
    # can tag ``is_current`` without re-decoding the header.
    if jti:
        setattr(request.state, "_auth_jwt_jti", str(jti))
    # Best-effort last_seen bump. Swallow failures so a broken sessions
    # table can never 500 an authed request.
    if jti:
        try:
            await touch_session_last_seen(db, str(jti))
        except Exception as exc:  # noqa: BLE001
            log.debug("session last_seen bump failed: %s", exc)
    return user


async def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """403 unless the current user has ``is_admin = True``."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


async def require_verified_email(
    user: User = Depends(get_current_user),
) -> User:
    """Block sensitive operations until email is verified.

    Not wired into any router yet — the MVP allows unverified users to
    sign in and explore. Enable on endpoints that hit shared resources
    (share / invite / public-link) once the full feature set lands.
    """
    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email verification required for this action",
        )
    return user


def _current_api_token(request: Request) -> ApiToken | None:
    """Return the :class:`ApiToken` used for this request, if any."""
    return getattr(request.state, _API_TOKEN_KEY, None)


def current_token_user_id(request: Request) -> int | None:
    """Return the ``user_id`` column of the API token used for this request.

    Reads directly from ``ApiToken.user_id`` rather than dereferencing the
    eager-loaded :attr:`ApiToken.user` relationship. The column read is
    safe inside any async greenlet — the relationship load is not
    guaranteed to be present once the row leaves its originating session
    (the rate-limit dep returns the User but downstream handlers receive
    a fresh DB session). Used by ``/api/events*`` to stamp
    ``Batch.owner_id`` deterministically from the token rather than
    indirectly via ``user.id`` (which depended on the relationship being
    loaded; #127).
    """
    row = _current_api_token(request)
    return row.user_id if row is not None else None


def _current_auth_kind(request: Request) -> str | None:
    """Return ``'jwt'`` or ``'api_token'`` for the current request."""
    return getattr(request.state, _AUTH_KIND_KEY, None)


async def require_reporter_token(
    request: Request,
    user: User = Depends(get_current_user),
) -> User:
    """Gate an endpoint behind an ``em_live_`` (scope=reporter) token.

    JWT callers cannot satisfy this dep — the ingest path is strictly
    for API tokens so we get per-token rate limiting and a stable
    "reporter identity" independent of browser sessions.

    Returns the :class:`User` that owns the token so the caller can
    stamp ``owner_id`` on newly-created rows.
    """
    token_row = _current_api_token(request)
    if token_row is None:
        # JWT was presented, not an API token.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reporter scope requires a personal API token",
        )
    if token_row.scope != "reporter":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This token has 'viewer' scope; POST requires 'reporter' "
                "(em_live_) tokens."
            ),
        )
    return user


async def require_web_session(
    request: Request,
    user: User = Depends(get_current_user),
) -> User:
    """Restrict an endpoint to JWT-authenticated (browser) callers.

    Used for ops that should never be driven by an opaque long-lived
    token: password change, token revoke, share management, etc.
    """
    if _current_auth_kind(request) != "jwt":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an interactive login session",
        )
    return user


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


async def enforce_ingest_rate_limit(
    request: Request,
    user: User = Depends(require_reporter_token),
) -> User:
    """Bucket-limit ingest calls per API token.

    The key is the token hash for API-token auth (so revoking a token
    also clears its rate state when the process recycles). JWT callers
    can't reach the ingest path — they fail in
    :func:`require_reporter_token` first — but for defence-in-depth we
    fall back to the user id.

    On 429 we emit ``Retry-After`` per requirements §6.4. We round
    *up* so the client backs off strictly long enough for a refill.
    """
    token_row = _current_api_token(request)
    key = (
        f"tok:{token_row.token_hash}"
        if token_row is not None
        else f"usr:{user.id}"
    )
    bucket = get_default_bucket()
    allowed, retry_after = await bucket.try_consume(key)
    if not allowed:
        retry_seconds = max(1, int(retry_after + 0.999))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(retry_seconds)},
        )
    return user
