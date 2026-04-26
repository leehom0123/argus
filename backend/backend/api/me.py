"""``/api/me/*`` user-self endpoints (#108).

These cover the per-user surfaces the Settings UI talks to that don't
fit cleanly under :mod:`backend.api.auth` (auth flow) or
:mod:`backend.api.notifications_email` (per-(project, event_type)
matrix). Specifically:

* ``GET / PUT /api/me/notification_prefs`` — the five-toggle defaults
  the user can flip from Settings → Notifications. They are *defaults*
  for new batches; the per-batch ``batch_email_subscription`` row, when
  present, always takes precedence at dispatch time.
* ``POST /api/me/resend_verification`` — re-issues the email-verify
  token + mails it. Rate-limited to 1/min/user (see
  :func:`backend.utils.ratelimit.get_resend_verification_bucket`).
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.deps import (
    get_current_user,
    get_db,
    get_email_service_dep,
    get_settings_dep,
)
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import EmailVerification, User
from backend.schemas.auth import OkResponse
from backend.schemas.me import NotificationPrefsIn, NotificationPrefsOut
from backend.services.audit import get_audit_service
from backend.services.email import EmailService
from backend.utils.ratelimit import get_resend_verification_bucket

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me", tags=["me"])


# ---------------------------------------------------------------------------
# Notification preference defaults
# ---------------------------------------------------------------------------

# Canonical defaults used when ``user.notification_prefs_json`` is NULL —
# kept consistent with the per-batch defaults the UI ships with on
# ``BatchDetail.vue`` (every event kind on except ``job_idle_flagged``,
# which is the noisiest of the five).
DEFAULT_PREFS: dict[str, bool] = {
    "notify_batch_done": True,
    "notify_batch_failed": True,
    "notify_job_failed": True,
    "notify_diverged": True,
    "notify_job_idle": False,
}

_PREF_KEYS: tuple[str, ...] = tuple(DEFAULT_PREFS.keys())


def _decode_prefs(raw: str | None) -> dict[str, bool]:
    """Parse the JSON column, gracefully ignoring corruption.

    Unknown keys in the stored payload are silently dropped so a future
    schema reduction doesn't surface stale toggles to the UI; missing
    keys fall back to :data:`DEFAULT_PREFS`. Anything that fails to
    parse at all is treated as "no customisation" — we'd rather show
    defaults than 500 the Settings page.
    """
    if not raw:
        return dict(DEFAULT_PREFS)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("notification_prefs: malformed JSON, using defaults")
        return dict(DEFAULT_PREFS)
    if not isinstance(parsed, dict):
        return dict(DEFAULT_PREFS)
    out = dict(DEFAULT_PREFS)
    for k in _PREF_KEYS:
        v = parsed.get(k)
        if isinstance(v, bool):
            out[k] = v
    return out


@router.get(
    "/notification_prefs",
    response_model=NotificationPrefsOut,
    summary="Read the caller's email-notification defaults",
)
async def get_notification_prefs(
    user: User = Depends(get_current_user),
) -> NotificationPrefsOut:
    """Return the five-toggle defaults, falling back to canonical values.

    The frontend renders these as defaults that propagate to new
    batches; per-batch overrides on ``batch_email_subscription`` win at
    dispatch time, so flipping a toggle here does NOT retroactively
    silence existing per-batch subscriptions.
    """
    prefs = _decode_prefs(user.notification_prefs_json)
    return NotificationPrefsOut(**prefs)


@router.put(
    "/notification_prefs",
    response_model=NotificationPrefsOut,
    summary="Replace the caller's email-notification defaults",
)
async def put_notification_prefs(
    body: NotificationPrefsIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationPrefsOut:
    """Total-update semantics: the body fully replaces the stored row.

    Pydantic enforces every key being present (see
    :class:`NotificationPrefsIn`) so partial-write ambiguity is
    impossible — round-tripping a GET → PUT works exactly. We persist
    a compact JSON literal (sorted keys, no whitespace) for stable
    on-disk diffs.
    """
    payload = body.model_dump()
    user.notification_prefs_json = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    await db.commit()

    # Audit log lives in a separate transaction-friendly call so a DB
    # outage on the audit table can't roll back the actual pref save.
    ip = request.client.host if request.client else None
    get_audit_service().log_background(
        action="notification_prefs_updated",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata=payload,
        ip=ip,
    )
    return NotificationPrefsOut(**payload)


# ---------------------------------------------------------------------------
# Resend verification email
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_frontend_url(base_url: str, path: str, token: str) -> str:
    """Mirror :func:`backend.api.auth._build_frontend_url`.

    Duplicated rather than imported because the auth module's helper is
    a private symbol; copying the four-line body avoids coupling the
    /me router to internal layout choices over there.
    """
    base = base_url.rstrip("/") + "/"
    return urljoin(base, path.lstrip("/")) + f"?token={token}"


@router.post(
    "/resend_verification",
    response_model=OkResponse,
    summary="Resend the email-verification link to the caller",
)
async def resend_verification(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    email_service: EmailService = Depends(get_email_service_dep),
    settings: Settings = Depends(get_settings_dep),
    locale: SupportedLocale = Depends(get_locale),
) -> OkResponse:
    """Re-issue the verify-email token + dispatch the templated email.

    Behaviour:

    * 200 ``{ok: true}`` on success (token row added, email queued).
    * 409 if the user is already verified — clients can branch on this
      to swap the banner for an "already verified" toast without an
      extra GET /auth/me round-trip.
    * 429 + ``Retry-After`` if the per-user 1/min bucket is empty.

    Token TTL mirrors :data:`Settings.email_verify_ttl_hours` (the
    register-flow default) so admins only have one knob to tune.
    """
    ip = request.client.host if request.client else None
    audit = get_audit_service()

    # ---- 1. short-circuit: already verified ------------------------------
    # Done before the rate-limit consume so a verified user spamming the
    # button doesn't lock themselves out of legit future resends (in case
    # they later run the change-email flow).
    if user.email_verified:
        audit.log_background(
            action="resend_verification_noop",
            user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            metadata={"reason": "already_verified"},
            ip=ip,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=tr(locale, "auth.verify.already_verified"),
        )

    # ---- 2. rate limit (1/min/user) --------------------------------------
    bucket = get_resend_verification_bucket()
    rl_key = f"rate-resend-verification:{user.id}"
    allowed, retry_after = await bucket.try_consume(rl_key)
    if not allowed:
        retry_seconds = max(1, int(retry_after + 0.999))
        audit.log_background(
            action="resend_verification_rate_limited",
            user_id=user.id,
            target_type="user",
            target_id=str(user.id),
            metadata={"retry_after_s": retry_seconds},
            ip=ip,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=tr(locale, "auth.verify.rate_limited"),
            headers={"Retry-After": str(retry_seconds)},
        )

    # ---- 3. mint a fresh token + persist ---------------------------------
    # We always issue a brand-new token rather than re-mailing the most
    # recent unconsumed one. Two reasons:
    #   1. Old tokens may have expired between request and response.
    #   2. If the original email leaked (forwarded mailbox, screenshot)
    #      a fresh token shrinks the attack window — stale rows expire
    #      naturally and the GC sweep cleans them up.
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    db.add(
        EmailVerification(
            token=token,
            user_id=user.id,
            kind="verify",
            created_at=_iso(now),
            expires_at=_iso(
                now + timedelta(hours=settings.email_verify_ttl_hours)
            ),
            consumed=False,
        )
    )
    await db.commit()

    # ---- 4. fire the email ----------------------------------------------
    # Match the registration flow: best-effort, never let an SMTP outage
    # surface as a 500 to the user (token is already valid).
    verify_url = _build_frontend_url(settings.base_url, "/verify-email", token)
    try:
        await email_service.send_verification(
            to=user.email,
            verify_url=verify_url,
            username=user.username,
            locale=user.preferred_locale or locale,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "resend_verification: dispatch failed for user %d: %s",
            user.id,
            exc,
        )

    audit.log_background(
        action="resend_verification_sent",
        user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        metadata=None,
        ip=ip,
    )
    return OkResponse(ok=True)
