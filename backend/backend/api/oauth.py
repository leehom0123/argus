"""GitHub OAuth login + bind/unbind endpoints.

Mounted at ``/api/auth/oauth/github/*`` alongside the existing local
email+password flow (see :mod:`backend.api.auth`). Two distinct flows
live behind the same callback, distinguished by the signed-nonce
``intent`` field:

* Login (``intent=login``) — browser hits ``/github/start`` while
  unauthenticated; callback mints a JWT and bounces to
  ``/login/oauth/complete#token=…``.
* Link (``intent=link``) — authenticated user clicks "Link GitHub" on
  the settings page; ``/github/link/start`` encodes their ``user_id``
  into the signed nonce; callback attaches ``github_id`` to that row
  and bounces to ``/login/oauth/complete?bind_ok=1``.

Defence in depth (unchanged from the login-only version):
* The feature is off by default — the start endpoints 404 unless both
  ``oauth.github_client_id`` and ``oauth.github_client_secret`` resolve
  via :func:`backend.services.runtime_config.get_config` (DB row > env
  > default), so a half-configured deployment never surfaces a button
  that 503s on click.
* State cookie is ``HttpOnly + SameSite=Lax`` + ``Secure`` in prod,
  scoped to ``/api/auth/oauth`` with a 10-minute TTL.
* State is HMAC-signed over the full nonce (incl. intent + user_id),
  so neither field can be tampered with in transit.
* No secret is logged; raw codes/tokens never leak to ``?reason=``.

Also adds two post-link management endpoints:

* ``POST /github/unlink`` — clears ``github_id`` / ``github_login``.
  Blocks when ``password_hash IS NULL`` so a GitHub-only user can't
  orphan themselves.
* ``POST /github/set-password`` — one-time password setup for GitHub-
  only users, so they can satisfy the unlink guard.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.jwt import create_access_token
from backend.auth.password import hash_password
from backend.config import Settings
from backend.deps import get_current_user, get_db, get_settings_dep
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import User
from backend.schemas.auth import _validate_password_strength
from backend.services.audit import get_audit_service
from backend.services.runtime_config import get_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/oauth", tags=["auth"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

STATE_COOKIE_NAME = "em_oauth_state"
STATE_COOKIE_TTL_SECONDS = 600
STATE_COOKIE_PATH = "/api/auth/oauth"

FRONTEND_COMPLETE_PATH = "/login/oauth/complete"
FRONTEND_ERROR_PATH = "/login"

HTTPX_TIMEOUT = 10.0

# Sanitisation for ``github_login`` → local ``username``.
_USERNAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sign_state(nonce: str, secret: str) -> str:
    """Return ``nonce.hmac(secret, nonce)``.

    The ``nonce`` itself may carry structured payload — e.g.
    ``"<random>~link~<user_id>"`` — and the HMAC covers the whole
    prefix so the intent / user_id fields can't be tampered with.
    """
    mac = hmac.new(
        secret.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{nonce}.{mac}"


def _verify_state(state: str, cookie_nonce: str, secret: str) -> bool:
    """Return True iff ``state`` is ``cookie_nonce + valid-HMAC``."""
    if not state or not cookie_nonce or "." not in state:
        return False
    nonce_part, _, mac_part = state.partition(".")
    if nonce_part != cookie_nonce:
        return False
    expected = _sign_state(cookie_nonce, secret).split(".", 1)[1]
    try:
        return hmac.compare_digest(expected, mac_part)
    except Exception:  # noqa: BLE001
        return False


def _build_nonce(intent: str, user_id: int | None = None) -> str:
    """Return a signed-intent nonce of the form ``<rand>~<intent>[~<uid>]``.

    ``~`` is not in the URL-safe base64 alphabet used by
    :func:`secrets.token_urlsafe`, so parsing is unambiguous.
    """
    parts = [secrets.token_urlsafe(18), intent]
    if user_id is not None:
        parts.append(str(int(user_id)))
    return "~".join(parts)


def _parse_nonce_intent(nonce: str) -> tuple[str, int | None]:
    """Return ``(intent, user_id?)`` parsed from ``nonce``.

    Unknown / legacy shapes default to ``("login", None)`` so the
    existing login flow keeps working even if a stale cookie is in
    flight during the rollout.
    """
    if not nonce or "~" not in nonce:
        return "login", None
    parts = nonce.split("~")
    intent = parts[1] if len(parts) > 1 else "login"
    if intent not in ("login", "link"):
        intent = "login"
    user_id: int | None = None
    if len(parts) > 2 and parts[2].isdigit():
        try:
            user_id = int(parts[2])
        except ValueError:
            user_id = None
    return intent, user_id


@dataclass(frozen=True)
class _GithubOAuthState:
    """Snapshot of GitHub OAuth runtime config from a single DB read.

    ``enabled`` is the derived "fully configured" flag — both
    ``client_id`` and ``client_secret`` must resolve via DB-or-env for
    the start endpoints to light up. Presence of the client_id alone
    is *not* enough: without the secret the token exchange would 503
    on click.
    """

    enabled: bool
    client_id: str | None
    client_secret: str | None


async def _get_github_oauth_state(db: AsyncSession) -> _GithubOAuthState:
    """Return GitHub OAuth runtime state with one batched DB read.

    Reads both ``oauth.github_client_id`` and
    ``oauth.github_client_secret`` via
    :func:`backend.services.runtime_config.get_config` so the DB row
    (when set by the admin UI) wins over the env-var fallback. Folds
    the prior ``_get_oauth_credentials`` + ``_is_github_configured``
    pair into a single helper so each public endpoint hits the
    config table at most once per request.
    """
    client_id = await get_config(db, "oauth", "github_client_id") or None
    client_secret = (
        await get_config(db, "oauth", "github_client_secret") or None
    )
    return _GithubOAuthState(
        enabled=bool(client_id) and bool(client_secret),
        client_id=client_id,
        client_secret=client_secret,
    )


async def _require_enabled(
    state: _GithubOAuthState, locale: SupportedLocale
) -> None:
    if not state.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=tr(locale, "auth.oauth.not_found"),
        )


def _sanitise_username_base(github_login: str) -> str:
    cleaned = _USERNAME_SAFE_RE.sub("", github_login or "") or "ghuser"
    if len(cleaned) < 3:
        cleaned = (cleaned + "ghuser")[:3]
    return cleaned[:28]


async def _allocate_unique_username(db: AsyncSession, base: str) -> str:
    candidate = base
    for n in range(0, 200):
        if n > 0:
            candidate = f"{base}-{n}"
        hit = await db.execute(
            select(User.id).where(func.lower(User.username) == candidate.lower())
        )
        if hit.scalar_one_or_none() is None:
            return candidate
    return f"{base}-{secrets.token_hex(4)}"


def _frontend_redirect(
    base_url: str, path: str, *, fragment: str | None = None,
    query: dict[str, str] | None = None,
) -> str:
    base = base_url.rstrip("/") + "/"
    url = urljoin(base, path.lstrip("/"))
    if query:
        url = f"{url}?{urlencode(query)}"
    if fragment:
        url = f"{url}#{fragment}"
    return url


def _error_redirect(
    settings: Settings,
    reason: str,
    redirect_to: str | None = None,
    *,
    intent: str = "login",
) -> RedirectResponse:
    """302 back to the appropriate error surface.

    For ``intent='link'`` we land on the OAuth-complete SPA route with
    ``bind_error=<reason>`` so the SPA can route to /settings/profile
    and show the toast there rather than bouncing through /login.
    """
    if intent == "link":
        query = {"bind_error": reason}
        url = _frontend_redirect(
            settings.base_url, FRONTEND_COMPLETE_PATH, query=query
        )
    else:
        query = {"error": "oauth_github_failed", "reason": reason}
        if redirect_to:
            query["redirect"] = redirect_to
        url = _frontend_redirect(
            settings.base_url, FRONTEND_ERROR_PATH, query=query
        )
    resp = RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    resp.delete_cookie(STATE_COOKIE_NAME, path=STATE_COOKIE_PATH)
    resp.delete_cookie(
        f"{STATE_COOKIE_NAME}_redirect", path=STATE_COOKIE_PATH
    )
    return resp


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/config")
async def oauth_config(
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    state = await _get_github_oauth_state(db)
    return {"github": state.enabled}


def _issue_oauth_redirect(
    settings: Settings,
    *,
    client_id: str,
    intent: str,
    user_id: int | None,
    redirect: str | None,
) -> RedirectResponse:
    """Shared: mint signed state + cookie + 302 to GitHub."""
    nonce = _build_nonce(intent, user_id=user_id)
    state = _sign_state(nonce, settings.jwt_secret)
    callback_url = _frontend_redirect(
        settings.base_url, "/api/auth/oauth/github/callback"
    )
    params = {
        "client_id": client_id or "",
        "redirect_uri": callback_url,
        "scope": "read:user user:email",
        "state": state,
        "allow_signup": "true" if intent == "login" else "false",
    }
    target = f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"
    resp = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    secure_flag = settings.env == "prod"
    resp.set_cookie(
        key=STATE_COOKIE_NAME,
        value=nonce,
        max_age=STATE_COOKIE_TTL_SECONDS,
        path=STATE_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=secure_flag,
    )
    if redirect and redirect.startswith("/") and not redirect.startswith("//"):
        resp.set_cookie(
            key=f"{STATE_COOKIE_NAME}_redirect",
            value=redirect,
            max_age=STATE_COOKIE_TTL_SECONDS,
            path=STATE_COOKIE_PATH,
            httponly=True,
            samesite="lax",
            secure=secure_flag,
        )
    return resp


@router.get("/github/start")
async def github_start(
    request: Request,
    redirect: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    locale: SupportedLocale = Depends(get_locale),
) -> RedirectResponse:
    """302 to GitHub with signed state cookie (login intent)."""
    state = await _get_github_oauth_state(db)
    await _require_enabled(state, locale)
    return _issue_oauth_redirect(
        settings,
        client_id=state.client_id or "",
        intent="login",
        user_id=None,
        redirect=redirect,
    )


@router.get("/github/link/start")
@router.post("/github/link/start")
async def github_link_start(
    request: Request,
    redirect: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    locale: SupportedLocale = Depends(get_locale),
    user: User = Depends(get_current_user),
) -> RedirectResponse:
    """Authenticated: start the "bind GitHub to this account" flow.

    Encodes ``user.id`` into the signed nonce so the callback knows
    which row to update. Supports GET (SPA hard-nav) + POST (form).

    .. deprecated::
        Prefer ``POST /github/link/init`` which returns JSON so the
        frontend can do ``window.location.href = authorize_url`` without
        needing to attach a bearer token to a browser navigation.
    """
    state = await _get_github_oauth_state(db)
    await _require_enabled(state, locale)
    if not redirect or not redirect.startswith("/") or redirect.startswith("//"):
        redirect = "/settings/profile"
    return _issue_oauth_redirect(
        settings,
        client_id=state.client_id or "",
        intent="link",
        user_id=user.id,
        redirect=redirect,
    )


class _LinkInitResponse(BaseModel):
    """Response body for ``POST /github/link/init``."""

    authorize_url: str


@router.post("/github/link/init")
async def github_link_init(
    request: Request,
    response: Response,
    redirect: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    locale: SupportedLocale = Depends(get_locale),
    user: User = Depends(get_current_user),
) -> _LinkInitResponse:
    """Authenticated JSON endpoint that begins the "bind GitHub" flow.

    The browser **cannot** send an ``Authorization: Bearer …`` header on
    a hard navigation (``window.location.href``), so the old
    ``GET /link/start`` returned 401 for every SPA-initiated link click.

    This endpoint is called by ``axios`` (which *does* send the bearer
    header), mints the signed state nonce, sets the ``HttpOnly`` state
    cookie on the response, and returns the full GitHub authorize URL as
    JSON.  The frontend then does::

        const { authorize_url } = await githubLinkStart();
        window.location.href = authorize_url;

    The state cookie travels to GitHub and back via the browser, so the
    callback validation path is unchanged.
    """
    oauth_state = await _get_github_oauth_state(db)
    await _require_enabled(oauth_state, locale)
    if not redirect or not redirect.startswith("/") or redirect.startswith("//"):
        redirect = "/settings/profile"

    nonce = _build_nonce("link", user_id=user.id)
    state = _sign_state(nonce, settings.jwt_secret)
    callback_url = _frontend_redirect(
        settings.base_url, "/api/auth/oauth/github/callback"
    )
    params = {
        "client_id": oauth_state.client_id or "",
        "redirect_uri": callback_url,
        "scope": "read:user user:email",
        "state": state,
        "allow_signup": "false",
    }
    authorize_url = f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"

    secure_flag = settings.env == "prod"
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=nonce,
        max_age=STATE_COOKIE_TTL_SECONDS,
        path=STATE_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=secure_flag,
    )
    response.set_cookie(
        key=f"{STATE_COOKIE_NAME}_redirect",
        value=redirect,
        max_age=STATE_COOKIE_TTL_SECONDS,
        path=STATE_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=secure_flag,
    )
    return _LinkInitResponse(authorize_url=authorize_url)


async def _exchange_code_for_token(
    code: str,
    settings: Settings,
    *,
    client_id: str | None,
    client_secret: str | None,
) -> str | None:
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": _frontend_redirect(
            settings.base_url, "/api/auth/oauth/github/callback"
        ),
    }
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            resp = await client.post(
                GITHUB_TOKEN_URL, data=data, headers=headers
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("github token exchange network failure: %r", exc)
        return None
    if resp.status_code >= 400:
        log.warning("github token exchange http %d", resp.status_code)
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        log.warning("github token exchange returned non-JSON")
        return None
    token = body.get("access_token")
    if not token or not isinstance(token, str):
        log.info(
            "github token exchange rejected (error=%s)",
            body.get("error", "unknown"),
        )
        return None
    return token


async def _fetch_github_profile(
    access_token: str,
) -> tuple[dict[str, Any] | None, str | None]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
            u_resp = await client.get(GITHUB_USER_URL, headers=headers)
            e_resp = await client.get(GITHUB_EMAILS_URL, headers=headers)
    except Exception as exc:  # noqa: BLE001
        log.warning("github profile fetch network failure: %r", exc)
        return None, None
    if u_resp.status_code >= 400:
        log.warning("github /user http %d", u_resp.status_code)
        return None, None
    try:
        user_json = u_resp.json()
    except Exception:  # noqa: BLE001
        return None, None
    primary_email: str | None = None
    try:
        emails = e_resp.json() if e_resp.status_code < 400 else []
        if isinstance(emails, list):
            for row in emails:
                if (
                    isinstance(row, dict)
                    and row.get("primary")
                    and row.get("verified")
                    and isinstance(row.get("email"), str)
                ):
                    primary_email = row["email"].lower()
                    break
    except Exception:  # noqa: BLE001
        primary_email = None
    if not primary_email and isinstance(user_json.get("email"), str):
        primary_email = user_json["email"].lower()
    return user_json, primary_email


@router.get("/github/callback")
async def github_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    locale: SupportedLocale = Depends(get_locale),
) -> Response:
    """Consume the OAuth code + state, then either log in or link."""
    oauth_state = await _get_github_oauth_state(db)
    await _require_enabled(oauth_state, locale)

    cookie_nonce = request.cookies.get(STATE_COOKIE_NAME, "")
    intent, link_user_id = _parse_nonce_intent(cookie_nonce)

    if not code:
        return _error_redirect(settings, "code_missing", intent=intent)

    if not state or not _verify_state(state, cookie_nonce, settings.jwt_secret):
        return _error_redirect(settings, "state_invalid", intent=intent)

    access_token = await _exchange_code_for_token(
        code,
        settings,
        client_id=oauth_state.client_id,
        client_secret=oauth_state.client_secret,
    )
    if not access_token:
        return _error_redirect(settings, "exchange_failed", intent=intent)

    user_json, primary_email = await _fetch_github_profile(access_token)
    if not user_json or "id" not in user_json:
        return _error_redirect(settings, "user_fetch_failed", intent=intent)
    if not primary_email:
        return _error_redirect(settings, "no_verified_email", intent=intent)

    github_id = str(user_json["id"])
    github_login = str(user_json.get("login") or "")
    ip = request.client.host if request.client else None

    if intent == "link":
        return await _handle_link_callback(
            request=request,
            db=db,
            settings=settings,
            link_user_id=link_user_id,
            github_id=github_id,
            github_login=github_login,
            ip=ip,
        )

    # ---- Login-flow: resolve / create user --------------------------
    existing_by_gh = (
        await db.execute(
            select(User).where(User.github_id == github_id)
        )
    ).scalar_one_or_none()

    user: User
    if existing_by_gh is not None:
        user = existing_by_gh
        if github_login and user.github_login != github_login:
            user.github_login = github_login
    else:
        existing_by_email = (
            await db.execute(
                select(User).where(User.email == primary_email)
            )
        ).scalar_one_or_none()
        if existing_by_email is not None:
            user = existing_by_email
            user.github_id = github_id
            user.github_login = github_login or user.github_login
            user.email_verified = True
        else:
            total_users = await db.scalar(
                select(func.count()).select_from(User)
            )
            is_first = (total_users or 0) == 0

            base_username = _sanitise_username_base(github_login)
            final_username = await _allocate_unique_username(
                db, base_username
            )
            user = User(
                username=final_username,
                email=primary_email,
                password_hash=None,
                is_active=True,
                is_admin=is_first,
                email_verified=True,
                created_at=_utcnow_iso(),
                failed_login_count=0,
                preferred_locale="en-US",
                github_id=github_id,
                github_login=github_login or None,
                auth_provider="github",
            )
            db.add(user)

    user.last_login = _utcnow_iso()
    await db.flush()

    await get_audit_service().log(
        action="login_success",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={
            "username": user.username,
            "via": "github",
            "github_id": github_id,
            "github_login": github_login or None,
        },
        ip=ip,
        db=db,
    )
    await db.commit()

    jwt_token, _exp, jti = create_access_token(user.id)
    # Track the issued JWT in active_sessions so the Settings > Sessions
    # panel can revoke OAuth-provisioned logins the same way as local
    # password logins. Non-fatal if the insert fails.
    try:
        from backend.auth.jwt import record_active_session  # noqa: PLC0415
        from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415

        await record_active_session(
            db,
            jti=jti,
            user_id=user.id,
            issued_at_epoch=int(_dt.now(_tz.utc).timestamp()),
            expires_at_epoch=_exp,
            user_agent=request.headers.get("user-agent"),
            ip=ip,
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        pass

    redirect_cookie = request.cookies.get(
        f"{STATE_COOKIE_NAME}_redirect", ""
    )
    fragment_parts = {
        "token": jwt_token,
        "email": user.email,
        "login": user.username,
    }
    if (
        redirect_cookie
        and redirect_cookie.startswith("/")
        and not redirect_cookie.startswith("//")
    ):
        fragment_parts["redirect"] = redirect_cookie

    target = _frontend_redirect(
        settings.base_url,
        FRONTEND_COMPLETE_PATH,
        fragment=urlencode(fragment_parts),
    )
    resp = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    resp.delete_cookie(STATE_COOKIE_NAME, path=STATE_COOKIE_PATH)
    resp.delete_cookie(
        f"{STATE_COOKIE_NAME}_redirect", path=STATE_COOKIE_PATH
    )
    return resp


async def _handle_link_callback(
    *,
    request: Request,
    db: AsyncSession,
    settings: Settings,
    link_user_id: int | None,
    github_id: str,
    github_login: str,
    ip: str | None,
) -> RedirectResponse:
    """Attach ``github_id`` to the user identified by ``link_user_id``.

    Never creates a new user, never logs in a different one. The
    integrity of ``link_user_id`` is guaranteed by the HMAC-signed
    nonce — forging would require the server secret. We still verify
    the target row exists and is active.
    """
    if link_user_id is None:
        return _error_redirect(settings, "state_invalid", intent="link")

    target = await db.get(User, int(link_user_id))
    if target is None or not target.is_active:
        return _error_redirect(settings, "state_invalid", intent="link")

    # Reject if this github_id is already linked to a different user.
    existing_by_gh = (
        await db.execute(
            select(User).where(User.github_id == github_id)
        )
    ).scalar_one_or_none()
    if existing_by_gh is not None and existing_by_gh.id != target.id:
        return _error_redirect(
            settings, "github_already_linked", intent="link"
        )

    # Idempotent: re-linking the same id is a no-op that still refreshes
    # the display handle in case the upstream changed.
    target.github_id = github_id
    target.github_login = github_login or target.github_login

    await get_audit_service().log(
        action="oauth_link",
        user_id=target.id,
        target_type="user",
        target_id=str(target.id),
        metadata={
            "via": "github",
            "github_id": github_id,
            "github_login": github_login or None,
        },
        ip=ip,
        db=db,
    )
    await db.commit()

    redirect_cookie = request.cookies.get(
        f"{STATE_COOKIE_NAME}_redirect", ""
    )
    query = {"bind_ok": "1"}
    if (
        redirect_cookie
        and redirect_cookie.startswith("/")
        and not redirect_cookie.startswith("//")
    ):
        query["redirect"] = redirect_cookie
    url = _frontend_redirect(
        settings.base_url, FRONTEND_COMPLETE_PATH, query=query
    )
    resp = RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    resp.delete_cookie(STATE_COOKIE_NAME, path=STATE_COOKIE_PATH)
    resp.delete_cookie(
        f"{STATE_COOKIE_NAME}_redirect", path=STATE_COOKIE_PATH
    )
    return resp


# ---------------------------------------------------------------------------
# Unlink + set-password (for github-only users)
# ---------------------------------------------------------------------------


class _SetPasswordIn(BaseModel):
    """Body for ``POST /github/set-password`` — one-time setup."""

    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(..., min_length=10, max_length=256)

    @field_validator("new_password")
    @classmethod
    def _check(cls, v: str) -> str:
        return _validate_password_strength(v)


@router.post(
    "/github/unlink",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def github_unlink(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    locale: SupportedLocale = Depends(get_locale),
) -> Response:
    """Detach the GitHub identity from the current user.

    Guards:
      * user must actually have a linked github_id (409 otherwise)
      * user must have a local password (409 otherwise) — without this
        we'd orphan a github-only user with no way back in. The SPA
        routes through /set-password first; this check is belt-and-
        braces for anyone hitting the endpoint directly.
    """
    if not user.github_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=tr(locale, "auth.oauth.not_linked"),
        )
    if not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=tr(locale, "auth.oauth.unlink_needs_password"),
        )

    prior_github_id = user.github_id
    prior_github_login = user.github_login
    user.github_id = None
    user.github_login = None
    user.auth_provider = "local"

    await get_audit_service().log(
        action="oauth_unlink",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={
            "via": "github",
            "github_id": prior_github_id,
            "github_login": prior_github_login,
        },
        ip=request.client.host if request.client else None,
        db=db,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/github/set-password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def github_set_password(
    payload: _SetPasswordIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    locale: SupportedLocale = Depends(get_locale),
) -> Response:
    """One-time password setup for users provisioned via GitHub.

    Only allowed when ``password_hash IS NULL``. Users who already have
    a password must use the regular change-password flow (not shipped
    in this PR).
    """
    if user.password_hash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=tr(locale, "auth.oauth.password_already_set"),
        )

    user.password_hash = hash_password(payload.new_password)
    user.failed_login_count = 0
    user.locked_until = None

    await get_audit_service().log(
        action="password_set_for_oauth_user",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={"via": "github"},
        ip=request.client.host if request.client else None,
        db=db,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
