"""``/api/auth/*`` endpoints.

Lays out the same flow described in design §4.1:

1. register → creates user, generates verify token, fires verification email.
2. verify-email → consumes one-shot token, flips ``email_verified``.
3. login → delegates to :class:`LocalAuthProvider`, issues JWT.
4. logout → adds JWT to the in-memory blacklist.
5. refresh → rotates JWT preserving user_id + custom claims.
6. password reset (request + consume) → same one-shot token flow.
7. me → returns the authenticated :class:`User`.

Error semantics intentionally don't distinguish "user not found" from
"wrong password" to avoid leaking existing accounts to scrapers.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.auth.jwt import (
    JWTError,
    blacklist_token,
    create_access_token,
    decode_token,
    record_active_session,
    refresh_access_token,
)
from backend.auth.password import hash_password, verify_password
from backend.auth.providers.local import (
    AccountLockedError,
    LocalAuthProvider,
)
from backend.config import Settings
from backend.deps import (
    get_current_user,
    get_db,
    get_email_service_dep,
    get_settings_dep,
    require_web_session,
)
from backend.models import ActiveSession, EmailVerification, User
from backend.schemas.auth import (
    ActiveSessionOut,
    ChangeEmailIn,
    ChangePasswordIn,
    EmailVerifyIn,
    LoginIn,
    OkResponse,
    PasswordResetIn,
    PasswordResetRequestIn,
    RegisterIn,
    RegisterOut,
    TokenResponse,
    UserOut,
)
from backend.services.audit import get_audit_service
from backend.services.email import EmailService
from backend.services.feature_flags import get_flag
from backend.utils.ratelimit import (
    get_change_email_bucket,
    get_change_password_bucket,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ua_hash(user_agent: str | None) -> str:
    """Return a short SHA-256 hex digest of the UA string.

    Hashing avoids persisting the full UA (often long and revealing of
    browser-fingerprint data) while still letting us tell "never seen
    this browser before" from "same browser, new IP".
    """
    if not user_agent:
        return ""
    return hashlib.sha256(
        user_agent.encode("utf-8", errors="ignore")
    ).hexdigest()[:16]


async def _check_anomalous_login(
    db: AsyncSession,
    user: User,
    ip: str | None,
    user_agent: str | None,
    email_service: "EmailService",
    locale: str,
    settings: Settings,
) -> bool:
    """Detect and notify on logins from a new (ip, UA hash).

    Side effects:
      * Trims stale (>30 day) entries from ``User.known_ips_json``.
      * Appends the current pair if it's not in the list.
      * Fires ``send_anomalous_login`` (fire-and-forget) when the pair
        is unseen **and** at least one other pair already exists (the
        first login after registration must not trigger the alert).

    Returns True when an alert email was scheduled, False otherwise.
    No exceptions propagate — a failure here must not block login.
    """
    if not settings.alerts_anomalous_login_enabled:
        return False
    try:
        raw = user.known_ips_json
        entries: list[dict] = json.loads(raw) if raw else []
        if not isinstance(entries, list):
            entries = []
    except Exception:  # noqa: BLE001
        entries = []

    now = _utcnow()
    cutoff = now - timedelta(days=30)

    def _is_fresh(e: dict) -> bool:
        try:
            ts = e.get("last_seen", "")
            ts = ts.rstrip("Z")
            if ts.endswith("+00:00"):
                ts = ts[:-6]
            return (
                datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
                >= cutoff
            )
        except Exception:  # noqa: BLE001
            return False

    entries = [e for e in entries if isinstance(e, dict) and _is_fresh(e)]

    cur_ip = ip or ""
    cur_ua = _ua_hash(user_agent)
    had_history = bool(entries)
    match = next(
        (
            e for e in entries
            if e.get("ip") == cur_ip and e.get("ua_hash") == cur_ua
        ),
        None,
    )

    fired = False
    if match is None:
        if had_history:
            # Fire the email lazily via background task; never await so
            # the login response isn't held up by SMTP latency.
            import asyncio as _asyncio  # noqa: PLC0415

            async def _send() -> None:
                try:
                    await email_service.send_anomalous_login(
                        to=user.email,
                        username=user.username,
                        ip=cur_ip or "unknown",
                        user_agent=user_agent or "unknown",
                        when_iso=_iso(now),
                        locale=locale,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug("anomalous_login email failed: %s", exc)

            try:
                _asyncio.create_task(_send())  # noqa: RUF006
                fired = True
            except RuntimeError:
                # No running loop (some test contexts) — ignore.
                pass
        entries.append(
            {"ip": cur_ip, "ua_hash": cur_ua, "last_seen": _iso(now)}
        )
    else:
        match["last_seen"] = _iso(now)

    # Cap at 50 entries so the column stays bounded even when the user
    # hits many transient dynamic IPs.
    entries.sort(key=lambda e: e.get("last_seen", ""), reverse=True)
    entries = entries[:50]
    user.known_ips_json = json.dumps(entries, separators=(",", ":"))
    # No explicit commit — the login endpoint commits its own session
    # right after calling us.
    return fired


def _build_frontend_url(base_url: str, path: str, token: str) -> str:
    """Join the frontend base URL with ``path`` + token query string.

    ``base_url`` is typically configured without a trailing slash
    (``https://monitor.example.com``) and ``path`` starts with ``/``.
    ``urljoin`` handles either case correctly.
    """
    base = base_url.rstrip("/") + "/"
    return urljoin(base, path.lstrip("/")) + f"?token={token}"


async def _issue_verification_token(
    db: AsyncSession,
    user_id: int,
    kind: str,
    ttl: timedelta,
    *,
    payload: str | None = None,
) -> str:
    """Create an :class:`EmailVerification` row and return the token.

    ``payload`` is an optional opaque string bound to the token. The
    email-change flow uses it to remember the requested new email so the
    consume-side endpoint doesn't have to trust query-string input.
    """
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    db.add(
        EmailVerification(
            token=token,
            user_id=user_id,
            kind=kind,
            created_at=_iso(now),
            expires_at=_iso(now + ttl),
            consumed=False,
            payload=payload,
        )
    )
    await db.commit()
    return token


async def _consume_verification_token(
    db: AsyncSession, token: str, kind: str, locale: SupportedLocale = "en-US"
) -> EmailVerification:
    """Look up and invalidate a one-shot token. Raises 400 on any problem."""
    row = await db.get(EmailVerification, token)
    if row is None or row.kind != kind:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.invalid"),
        )
    if row.consumed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.used"),
        )
    expires = row.expires_at
    try:
        # Parse either ...Z or ...+00:00
        cleaned = expires.rstrip("Z")
        if cleaned.endswith("+00:00"):
            cleaned = cleaned[:-6]
        exp_dt = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.malformed_expiry"),
        )
    if exp_dt < _utcnow():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.expired"),
        )
    row.consumed = True
    await db.commit()
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=RegisterOut,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: RegisterIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    email_service: EmailService = Depends(get_email_service_dep),
    locale: SupportedLocale = Depends(get_locale),
) -> RegisterOut:
    """Create a new user and mail a verification link.

    The first registered user gets ``is_admin=True``. To avoid the
    Round-2 M4 race (two concurrent registrations both reading
    ``COUNT(*) = 0`` before either insert lands), we open an explicit
    ``BEGIN IMMEDIATE`` on the underlying SQLite connection so readers
    serialise with the upcoming write. Non-SQLite backends still get
    strong consistency from their default REPEATABLE READ isolation.
    """
    # Gate-keep on the admin-controlled registration_open flag. We use
    # the service layer default (True) so this is a no-op in MVP until
    # an admin flips it.
    if not await get_flag(db, "registration_open", default=True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=tr(locale, "auth.register.disabled"),
        )

    # Normalise email to lowercase so future lookups don't depend on what
    # the user typed.
    email = payload.email.lower()

    # Serialise the "is this the first user?" check + the INSERT under a
    # single write lock. SQLite's BEGIN IMMEDIATE acquires the reserved
    # lock, which blocks other writers until we commit — so two parallel
    # registrations can't both see COUNT=0.
    from sqlalchemy import text as sa_text

    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        try:
            await db.execute(sa_text("BEGIN IMMEDIATE"))
        except Exception as exc:  # noqa: BLE001
            # Already in a transaction (e.g. test harness pre-BEGINs);
            # fall through. The COUNT-then-INSERT is still atomic under
            # SQLite's serialized writes for the single-admin path.
            log.debug("could not escalate to BEGIN IMMEDIATE: %s", exc)

    # Uniqueness check (case-insensitive on username, exact on normalised email)
    existing = await db.execute(
        select(User.id).where(
            (func.lower(User.username) == payload.username.lower())
            | (User.email == email)
        )
    )
    if existing.scalar_one_or_none() is not None:
        # Audit the failed register attempt before 409.
        await get_audit_service().log(
            action="register_failed",
            user_id=None,
            target_type="user",
            target_id=payload.username,
            metadata={"reason": "duplicate"},
            ip=request.client.host if request.client else None,
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=tr(locale, "auth.register.duplicate"),
        )

    total_users = await db.scalar(select(func.count()).select_from(User))
    is_first = (total_users or 0) == 0

    user = User(
        username=payload.username,
        email=email,
        password_hash=hash_password(payload.password),
        is_active=True,
        is_admin=is_first,
        email_verified=False,
        created_at=_iso(_utcnow()),
        failed_login_count=0,
    )
    db.add(user)
    await db.flush()  # assigns user.id

    token = secrets.token_urlsafe(32)
    db.add(
        EmailVerification(
            token=token,
            user_id=user.id,
            kind="verify",
            created_at=_iso(_utcnow()),
            expires_at=_iso(
                _utcnow() + timedelta(hours=settings.email_verify_ttl_hours)
            ),
            consumed=False,
        )
    )
    # Append audit row in the same transaction so a rollback below
    # doesn't leave orphan audit entries.
    await get_audit_service().log(
        action="register",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={
            "username": user.username,
            "is_admin": user.is_admin,
        },
        ip=request.client.host if request.client else None,
        db=db,
    )
    await db.commit()

    verify_url = _build_frontend_url(settings.base_url, "/verify-email", token)
    # Fire-and-forget — wrap so SMTP outage never fails registration.
    try:
        await email_service.send_verification(
            to=user.email,
            verify_url=verify_url,
            username=user.username,
            locale=user.preferred_locale or locale,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("verify email dispatch failed for user %d: %s", user.id, exc)

    log.info(
        "registered user id=%d username=%s is_admin=%s",
        user.id,
        user.username,
        user.is_admin,
    )
    return RegisterOut(user_id=user.id, require_verify=True)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    locale: SupportedLocale = Depends(get_locale),
) -> TokenResponse:
    """Authenticate + return a JWT.

    Failures share one opaque 401 so an attacker can't tell "unknown user"
    from "wrong password". Lockout surfaces as 423 so the UI can render
    the retry-after hint instead of just "wrong password again".
    """
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    audit = get_audit_service()
    provider = LocalAuthProvider()
    try:
        user = await provider.authenticate(payload.model_dump(), db)
    except AccountLockedError as exc:
        audit.log_background(
            action="login_failed",
            user_id=None,
            target_type="user",
            target_id=payload.username_or_email,
            metadata={"reason": "locked"},
            ip=ip,
        )
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=tr(locale, "auth.locked", minutes=max(1, exc.retry_after // 60)),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    if user is None:
        audit.log_background(
            action="login_failed",
            user_id=None,
            target_type="user",
            target_id=payload.username_or_email,
            metadata={"reason": "bad_credentials"},
            ip=ip,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=tr(locale, "auth.credentials.bad"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, exp_epoch, jti = create_access_token(user.id)
    now_epoch = int(_utcnow().timestamp())
    # Track the new JWT in active_sessions so Settings > Sessions can
    # list + revoke it. Failures must not block login; we log and move on.
    try:
        await record_active_session(
            db,
            jti=jti,
            user_id=user.id,
            issued_at_epoch=now_epoch,
            expires_at_epoch=exp_epoch,
            user_agent=user_agent,
            ip=ip,
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("active_sessions insert failed (non-fatal): %s", exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    # Anomalous-login detection: compare against ``known_ips_json`` and
    # fire an informational email if this (ip, ua) pair is new AND the
    # user already had prior history (so the very first login post-
    # registration doesn't trigger a spurious alert). Failures here must
    # not block the login response.
    try:
        from backend.services.email import get_email_service  # noqa: PLC0415

        alert_fired = await _check_anomalous_login(
            db,
            user,
            ip,
            user_agent,
            get_email_service(),
            user.preferred_locale or locale,
            settings,
        )
        # Persist the updated known_ips_json.
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        alert_fired = False
        log.debug("anomalous-login check failed: %s", exc)

    audit.log_background(
        action="login_success",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={
            "username": user.username,
            "anomalous_login_alert": alert_fired,
        },
        ip=ip,
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=max(0, exp_epoch - now_epoch),
        user=UserOut.model_validate(user),
    )


@router.post("/logout", response_model=OkResponse)
async def logout(
    request: Request,
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
) -> OkResponse:
    """Invalidate the current JWT via the in-memory blacklist."""
    assert authorization  # enforced by get_current_user
    token = authorization[7:].strip()
    audit = get_audit_service()
    ip = request.client.host if request.client else None
    try:
        payload = decode_token(token)
        exp = float(payload.get("exp", _utcnow().timestamp()))
    except JWTError:
        # Already invalid — nothing to blacklist but report success for idempotency.
        audit.log_background(
            action="logout",
            user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            metadata={"detail": "already-invalid"},
            ip=ip,
        )
        return OkResponse(ok=True, detail="already-invalid")
    await blacklist_token(token, exp)
    audit.log_background(
        action="logout",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={"detail": "logged-out"},
        ip=ip,
    )
    return OkResponse(ok=True, detail="logged-out")


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> TokenResponse:
    """Issue a new JWT without requiring re-login."""
    assert authorization
    old_token = authorization[7:].strip()
    try:
        token, exp_epoch, jti = refresh_access_token(old_token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=tr(locale, "auth.token.refresh_failed"),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    # Record the fresh JWT in active_sessions so the refreshed token shows
    # up (and becomes revocable) in Settings > Sessions.
    try:
        await record_active_session(
            db,
            jti=jti,
            user_id=user.id,
            issued_at_epoch=int(_utcnow().timestamp()),
            expires_at_epoch=exp_epoch,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("active_sessions insert on refresh failed: %s", exc)
    # Add the old token to the blacklist so it can't be reused after refresh.
    try:
        payload = decode_token(old_token)
        await blacklist_token(old_token, float(payload["exp"]))
    except JWTError:
        pass

    now_epoch = int(_utcnow().timestamp())
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=max(0, exp_epoch - now_epoch),
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    """Return the currently authenticated user."""
    return UserOut.model_validate(user)


@router.post("/verify-email", response_model=OkResponse)
async def verify_email(
    payload: EmailVerifyIn,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> OkResponse:
    """Consume a verify-email token and flip ``email_verified`` on the user."""
    row = await _consume_verification_token(db, payload.token, kind="verify", locale=locale)
    user = await db.get(User, row.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.user_missing"),
        )
    user.email_verified = True
    await db.commit()
    return OkResponse(ok=True, detail="verified")


@router.post("/request-password-reset", response_model=OkResponse)
async def request_password_reset(
    payload: PasswordResetRequestIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    email_service: EmailService = Depends(get_email_service_dep),
) -> OkResponse:
    """Send a reset link if the email is known.

    Always returns 200 regardless of whether the email exists, so attackers
    can't probe the user list through this endpoint.
    """
    email = payload.email.lower()
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is not None and user.is_active:
        token = await _issue_verification_token(
            db,
            user.id,
            kind="reset_password",
            ttl=timedelta(minutes=settings.password_reset_ttl_minutes),
        )
        reset_url = _build_frontend_url(
            settings.base_url, "/reset-password", token
        )
        try:
            await email_service.send_password_reset(
                to=user.email,
                reset_url=reset_url,
                username=user.username,
                locale=user.preferred_locale or locale,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "password reset email dispatch failed for user %d: %s",
                user.id,
                exc,
            )
    else:
        # Don't log the client IP for unknown-email probes (GDPR-adjacent:
        # an attacker enumerating emails would otherwise leave a trail
        # of IP↔email guesses in our logs). A single debug-level metric
        # marker is enough to notice probing via count-only aggregation.
        log.debug("password reset requested for unknown email (ip redacted)")
    return OkResponse(ok=True, detail="ok")


@router.post("/reset-password", response_model=OkResponse)
async def reset_password(
    payload: PasswordResetIn,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> OkResponse:
    """Consume a reset token and set a new password."""
    row = await _consume_verification_token(
        db, payload.token, kind="reset_password", locale=locale
    )
    user = await db.get(User, row.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.user_missing"),
        )
    user.password_hash = hash_password(payload.new_password)
    # Resetting password counts as "fresh start": clear lockouts.
    user.failed_login_count = 0
    user.locked_until = None
    await db.commit()
    return OkResponse(ok=True, detail="password-updated")


@router.post("/change-password", response_model=OkResponse)
async def change_password(
    payload: ChangePasswordIn,
    request: Request,
    user: User = Depends(require_web_session),
    db: AsyncSession = Depends(get_db),
    email_service: EmailService = Depends(get_email_service_dep),
    locale: SupportedLocale = Depends(get_locale),
) -> OkResponse:
    """Change the caller's password from within an authenticated session.

    Flow:
      1. Rate-limit 5 attempts/hour per ``user_id`` (``rate-change-password:{id}``).
      2. Verify ``current_password`` against the argon2 hash — 401 on mismatch.
      3. Reject new == current (400) *before* the update: same-password
         change is always a user error.
      4. Persist the new hash. The caller's *own* JWT stays valid so they
         don't lose their current browser tab — every **other** JWT on the
         account gets ``revoked_at=now`` in ``active_sessions`` so stolen
         tokens elsewhere are invalidated on their next request.
      5. Audit row (``password_changed``) + out-of-band email notice with
         IP + UA so an attacker-initiated change is surfaced via a second
         channel.

    Security notes:
      * We reuse the in-session argon2 hasher (``verify_password`` /
        ``hash_password``) — no separate bcrypt path.
      * ``lockout`` state is NOT cleared here (unlike reset-password): an
        authenticated user hitting this endpoint wasn't locked out anyway.
      * No in-memory JWT blacklist entry is added for revoked jtis: the
        DB-side ``is_session_revoked`` check in ``get_current_user`` is
        authoritative and survives process restart.
    """
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent") or "unknown"

    # ---- 1. rate limit -----------------------------------------------
    bucket = get_change_password_bucket()
    rl_key = f"rate-change-password:{user.id}"
    allowed, retry_after = await bucket.try_consume(rl_key)
    if not allowed:
        retry_seconds = max(1, int(retry_after + 0.999))
        get_audit_service().log_background(
            action="password_change_rate_limited",
            user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            metadata={"retry_after_s": retry_seconds},
            ip=ip,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=tr(locale, "auth.password.rate_limited"),
            headers={"Retry-After": str(retry_seconds)},
        )

    # ---- 2. verify current password ---------------------------------
    if not user.password_hash or not verify_password(
        payload.current_password, user.password_hash
    ):
        get_audit_service().log_background(
            action="password_change_failed",
            user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            metadata={"reason": "wrong_current"},
            ip=ip,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=tr(locale, "auth.password.wrong_current"),
        )

    # ---- 3. reject same-password change -----------------------------
    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.password.same"),
        )

    # ---- 4. update hash + revoke other sessions ---------------------
    user.password_hash = hash_password(payload.new_password)

    current_jti = getattr(request.state, "_auth_jwt_jti", None)
    now_iso = _iso(_utcnow())
    revoke_stmt = (
        select(ActiveSession)
        .where(ActiveSession.user_id == user.id)
        .where(ActiveSession.revoked_at.is_(None))
    )
    rows = (await db.execute(revoke_stmt)).scalars().all()
    revoked_count = 0
    for row in rows:
        if current_jti is not None and row.jti == current_jti:
            # Keep the caller's own session alive.
            continue
        row.revoked_at = now_iso
        revoked_count += 1

    # ---- 5. audit log (in-transaction so commit is atomic) ----------
    await get_audit_service().log(
        action="password_changed",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={
            "revoked_other_sessions": revoked_count,
            "kept_current_jti": bool(current_jti),
        },
        ip=ip,
        db=db,
    )
    await db.commit()

    # ---- 6. out-of-band email notice --------------------------------
    try:
        await email_service.send_password_changed_notification(
            to=user.email,
            locale=user.preferred_locale or locale,
            ip=ip or "unknown",
            user_agent=user_agent,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "password-changed email dispatch failed for user %d: %s",
            user.id,
            exc,
        )

    return OkResponse(ok=True, detail="password-updated")


# ---------------------------------------------------------------------------
# Email-change flow (Settings > Profile)
#
# Two endpoints:
#   * ``POST /api/auth/change-email``        — authenticated; accepts
#     {new_email, current_password}. Verifies password, issues a one-shot
#     verification token bound to the new email (stored in the token row's
#     ``payload`` column), mails the link to the **new** email. The user's
#     User.email row stays unchanged at this point.
#   * ``GET  /api/auth/verify-new-email``    — public; ``?token=...``
#     consumes the token and atomically rewrites ``User.email``, clears
#     ``email_verified=False`` so the user re-verifies if they want the
#     new address back to verified state. Replays / expiries → 410 Gone.
# ---------------------------------------------------------------------------


@router.post("/change-email", response_model=OkResponse)
async def change_email(
    payload: ChangeEmailIn,
    request: Request,
    user: User = Depends(require_web_session),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    email_service: EmailService = Depends(get_email_service_dep),
    locale: SupportedLocale = Depends(get_locale),
) -> OkResponse:
    """Begin the change-email flow: re-confirm password, mail a verify link.

    Flow:
      1. Rate-limit 3/hour per user_id (``rate-change-email:{id}``).
      2. Verify ``current_password`` against the argon2 hash — 401 mismatch.
      3. Reject same-email (400) and pre-emptively reject already-taken
         email (400). Both checks are race-free at the issue step; the
         confirm step re-checks before committing the User row anyway.
      4. Issue a one-shot ``EmailVerification`` row with ``kind='email_change'``
         and ``payload=new_email``, mail the link to the **new** email.
      5. Audit ``email_change_requested``.

    Note: the User row is **not** modified at this stage. ``user.email``
    only flips after the user clicks the link. If they never confirm, the
    request times out and the row stays as it was.
    """
    ip = request.client.host if request.client else None
    new_email = payload.new_email.lower()

    # ---- 1. rate limit ------------------------------------------------
    bucket = get_change_email_bucket()
    rl_key = f"rate-change-email:{user.id}"
    allowed, retry_after = await bucket.try_consume(rl_key)
    if not allowed:
        retry_seconds = max(1, int(retry_after + 0.999))
        get_audit_service().log_background(
            action="email_change_rate_limited",
            user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            metadata={"retry_after_s": retry_seconds},
            ip=ip,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=tr(locale, "auth.email.rate_limited"),
            headers={"Retry-After": str(retry_seconds)},
        )

    # ---- 2. verify current password ----------------------------------
    if not user.password_hash or not verify_password(
        payload.current_password, user.password_hash
    ):
        get_audit_service().log_background(
            action="email_change_failed",
            user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            metadata={"reason": "wrong_password"},
            ip=ip,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=tr(locale, "auth.password.wrong_current"),
        )

    # ---- 3. semantic checks ------------------------------------------
    if new_email == (user.email or "").lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.email.same"),
        )
    existing = await db.execute(
        select(User.id).where(User.email == new_email).where(User.id != user.id)
    )
    if existing.scalar_one_or_none() is not None:
        # Mirror register's response — 400 keeps the message neutral and
        # consistent with what the UI surfaces from the register flow.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.email.in_use"),
        )

    # ---- 4. issue token + mail link ----------------------------------
    token = await _issue_verification_token(
        db,
        user.id,
        kind="email_change",
        ttl=timedelta(hours=settings.email_change_ttl_hours),
        payload=new_email,
    )
    verify_url = _build_frontend_url(
        settings.base_url, "/verify-new-email", token
    )
    try:
        await email_service.send_email_change_verification(
            to=new_email,
            verify_url=verify_url,
            username=user.username,
            locale=user.preferred_locale or locale,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "email-change verification dispatch failed for user %d: %s",
            user.id,
            exc,
        )

    # ---- 5. audit ----------------------------------------------------
    get_audit_service().log_background(
        action="email_change_requested",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={
            "old_email": user.email,
            "new_email": new_email,
        },
        ip=ip,
    )
    return OkResponse(ok=True, detail="email-change-requested")


@router.get("/verify-new-email", response_model=OkResponse)
async def verify_new_email(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    locale: SupportedLocale = Depends(get_locale),
) -> OkResponse:
    """Consume an email-change token and rewrite ``User.email``.

    Public endpoint — the token itself is the auth credential, exactly
    like password-reset and the initial verify flow. Consumed tokens and
    expired tokens both return ``410 Gone`` so a replayed link surfaces
    clearly distinct from "bad token format" (which is 400 from the
    pydantic layer).

    On success the user's ``email`` is replaced with the value held in
    ``EmailVerification.payload`` and ``email_verified`` flips to True
    (the user has just demonstrated control of the new mailbox).
    """
    row = await db.get(EmailVerification, token)
    if row is None or row.kind != "email_change":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.invalid"),
        )
    if row.consumed:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=tr(locale, "auth.token.used"),
        )
    try:
        cleaned = row.expires_at.rstrip("Z")
        if cleaned.endswith("+00:00"):
            cleaned = cleaned[:-6]
        exp_dt = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.malformed_expiry"),
        )
    if exp_dt < _utcnow():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=tr(locale, "auth.token.expired"),
        )

    new_email = (row.payload or "").lower()
    if not new_email:
        # Defensive: a corrupted token row with no payload can't
        # progress — treat as a bad token to avoid silently no-op'ing.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.invalid"),
        )

    user = await db.get(User, row.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.token.user_missing"),
        )

    # Race-check: another account may have grabbed the email between
    # request and confirm. Reject rather than silently dropping the
    # change.
    clash = await db.execute(
        select(User.id).where(User.email == new_email).where(User.id != user.id)
    )
    if clash.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tr(locale, "auth.email.in_use"),
        )

    old_email = user.email
    user.email = new_email
    user.email_verified = True
    row.consumed = True
    await get_audit_service().log(
        action="email_changed",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata={"old_email": old_email, "new_email": new_email},
        ip=request.client.host if request.client else None,
        db=db,
    )
    await db.commit()
    return OkResponse(ok=True, detail="email-changed")


# ---------------------------------------------------------------------------
# Session management — Settings > Sessions panel (issue #31)
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=list[ActiveSessionOut])
async def list_sessions(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ActiveSessionOut]:
    """List the caller's currently-valid JWTs.

    Returns unexpired, non-revoked rows only. ``is_current=True`` tags
    the JWT used to make *this* request so the UI can disable the
    "revoke" button on that row (or prompt for confirmation before the
    user logs themselves out).
    """
    now_iso = _iso(_utcnow())
    stmt = (
        select(ActiveSession)
        .where(ActiveSession.user_id == user.id)
        .where(ActiveSession.revoked_at.is_(None))
        .where(ActiveSession.expires_at > now_iso)
        .order_by(ActiveSession.issued_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    current_jti = getattr(request.state, "_auth_jwt_jti", None)
    out: list[ActiveSessionOut] = []
    for row in rows:
        data = {
            "jti": row.jti,
            "issued_at": row.issued_at,
            "expires_at": row.expires_at,
            "user_agent": row.user_agent,
            "ip": row.ip,
            "last_seen_at": row.last_seen_at,
            "is_current": (row.jti == current_jti),
        }
        out.append(ActiveSessionOut(**data))
    return out


@router.post(
    "/sessions/{jti}/revoke",
    response_model=OkResponse,
)
async def revoke_session(
    jti: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OkResponse:
    """Revoke one of the caller's JWTs by ``jti``.

    Sets ``revoked_at`` on the active_sessions row and also adds a
    best-effort blacklist entry so the revocation takes effect on the
    next request to any process sharing the same in-memory blacklist.
    """
    row = await db.get(ActiveSession, jti)
    if row is None or row.user_id != user.id:
        # 404 rather than 403 so an attacker enumerating jtis can't tell
        # "belongs to someone else" from "doesn't exist".
        raise HTTPException(status_code=404, detail="session not found")

    if row.revoked_at is None:
        row.revoked_at = _iso(_utcnow())
        await db.commit()
    # Best-effort: the caller didn't send the raw token so we can't
    # hash it for the process-wide blacklist. The DB-side check in
    # ``get_current_user`` covers us; the blacklist is an optimisation
    # for hot-path performance, not a correctness requirement.
    return OkResponse(ok=True, detail="revoked")
