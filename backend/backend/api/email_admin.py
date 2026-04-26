"""/api/admin/email/* - admin-only email management endpoints.

Co-owned by Team Email:
* BE-1 owns schema + subscription CRUD + dispatcher hooks. Appends
  above the BE-2 section below.
* BE-2 owns SMTP test body, dead-letter list + retry, and /stats.
  Implementations in the BE-2 section at the bottom.

Rebase-friendly: the two sections are contiguous blocks so a typical
conflict resolves as "accept both".
"""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_db, require_admin
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import EmailTemplate, SmtpConfig, User
from backend.schemas.email import (
    EmailTemplateOut,
    EmailTemplatePreviewOut,
    EmailTemplateUpdateIn,
    MASKED_PASSWORD,
    SmtpConfigIn,
    SmtpConfigOut,
)
from backend.services.audit import get_audit_service
from backend.services.email_worker import EmailJob, enqueue, get_metrics
from backend.utils.ratelimit import get_smtp_test_bucket

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/email", tags=["admin", "email"])


# ---------------------------------------------------------------------------
# SMTP config CRUD (BE-1 owns this section)
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _smtp_to_out(row: SmtpConfig | None) -> SmtpConfigOut:
    if row is None:
        return SmtpConfigOut(
            enabled=False,
            smtp_host=None,
            smtp_port=587,
            smtp_username=None,
            smtp_password=MASKED_PASSWORD,
            smtp_from_address=None,
            smtp_from_name=None,
            use_tls=True,
            use_ssl=False,
            updated_at=None,
            updated_by_user_id=None,
        )
    return SmtpConfigOut(
        enabled=bool(row.enabled),
        smtp_host=row.smtp_host,
        smtp_port=int(row.smtp_port or 587),
        smtp_username=row.smtp_username,
        smtp_password=MASKED_PASSWORD,
        smtp_from_address=row.smtp_from_address,
        smtp_from_name=row.smtp_from_name,
        use_tls=bool(row.use_tls),
        use_ssl=bool(row.use_ssl),
        updated_at=getattr(row, "updated_at", None),
        updated_by_user_id=getattr(row, "updated_by_user_id", None),
    )


@router.get(
    "/smtp",
    response_model=SmtpConfigOut,
    summary="Read the singleton SMTP config row (password masked)",
)
async def get_smtp_config(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SmtpConfigOut:
    """Return the row at ``smtp_config.id=1`` or a defaulted shell."""
    row = await db.get(SmtpConfig, 1)
    return _smtp_to_out(row)


@router.put(
    "/smtp",
    response_model=SmtpConfigOut,
    summary="Upsert the singleton SMTP config row",
)
async def put_smtp_config(
    body: SmtpConfigIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SmtpConfigOut:
    """Create or update ``smtp_config.id=1``.

    A password value of ``"***"`` (the masked sentinel from the read
    path) is treated as "preserve the stored secret".  Anything else,
    including the empty string, replaces it.
    """
    row = await db.get(SmtpConfig, 1)
    if row is None:
        row = SmtpConfig(id=1)
        db.add(row)

    row.enabled = bool(body.enabled)
    row.smtp_host = body.smtp_host
    row.smtp_port = int(body.smtp_port)
    row.smtp_username = body.smtp_username
    if body.smtp_password != MASKED_PASSWORD:
        row.smtp_password_encrypted = body.smtp_password
    row.smtp_from_address = body.smtp_from_address
    row.smtp_from_name = body.smtp_from_name
    row.use_tls = bool(body.use_tls)
    row.use_ssl = bool(body.use_ssl)
    row.updated_at = _utcnow_iso()
    if hasattr(row, "updated_by_user_id"):
        row.updated_by_user_id = admin.id
    await db.commit()
    await db.refresh(row)
    return _smtp_to_out(row)


# ---------------------------------------------------------------------------
# Email template CRUD (BE-1)
# ---------------------------------------------------------------------------


def _template_to_out(row: EmailTemplate) -> EmailTemplateOut:
    return EmailTemplateOut(
        id=row.id,
        event_type=row.event_type,
        locale=row.locale,
        subject=row.subject,
        body_html=row.body_html,
        body_text=row.body_text,
        is_system=bool(row.is_system),
        updated_at=getattr(row, "updated_at", None),
        updated_by_user_id=getattr(row, "updated_by_user_id", None),
    )


@router.get(
    "/templates",
    response_model=list[EmailTemplateOut],
    summary="List every (event_type, locale) email template row",
)
async def list_templates(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[EmailTemplateOut]:
    rows = (
        await db.execute(
            select(EmailTemplate).order_by(
                EmailTemplate.event_type, EmailTemplate.locale
            )
        )
    ).scalars().all()
    return [_template_to_out(r) for r in rows]


@router.get(
    "/templates/{template_id}",
    response_model=EmailTemplateOut,
    summary="Read a single template by row id",
)
async def get_template(
    template_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> EmailTemplateOut:
    row = await db.get(EmailTemplate, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="template not found")
    return _template_to_out(row)


@router.put(
    "/templates/{template_id}",
    response_model=EmailTemplateOut,
    summary="Update subject + body fields of a template (event_type/locale immutable)",
)
async def update_template(
    template_id: int,
    body: EmailTemplateUpdateIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> EmailTemplateOut:
    row = await db.get(EmailTemplate, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="template not found")
    row.subject = body.subject
    row.body_html = body.body_html
    row.body_text = body.body_text
    row.updated_at = _utcnow_iso()
    if hasattr(row, "updated_by_user_id"):
        row.updated_by_user_id = admin.id
    await db.commit()
    await db.refresh(row)
    return _template_to_out(row)


@router.post(
    "/templates/{template_id}/preview",
    response_model=EmailTemplatePreviewOut,
    summary="Render a template against canned sample context for preview",
)
async def preview_template(
    template_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> EmailTemplatePreviewOut:
    row = await db.get(EmailTemplate, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="template not found")

    # Sample context covers every field touched by EVENT_DEFAULTS.
    # The dispatcher's _build_context produces a richer dict at run
    # time; the preview keeps the shape stable but the values fixed
    # so admins see what to expect.
    sample = {
        "user": {"username": "alice", "email": "alice@example.com"},
        "batch": {
            "id": "bench-sample",
            "project": "demo",
            "status": row.event_type.replace("batch_", "")
            if row.event_type.startswith("batch_") else "running",
            "n_done": 8,
            "n_failed": 1,
        },
        "job": {"id": "job-001", "error": "RuntimeError: out of memory"},
        "shared_by": {"username": "bob"},
        "project": "demo",
        "permission": "viewer",
        "link": {
            "batch_url": "https://example.org/batches/bench-sample",
            "project_url": "https://example.org/projects/demo",
            "unsubscribe_url": "https://example.org/unsubscribe?token=preview",
        },
    }

    try:
        from jinja2 import Environment, select_autoescape
    except ImportError:  # pragma: no cover - jinja2 is a hard dep
        raise HTTPException(
            status_code=500, detail="jinja2 unavailable; cannot render preview"
        )
    env = Environment(autoescape=select_autoescape(["html"]))
    try:
        rendered_subject = env.from_string(row.subject).render(**sample)
        rendered_html = env.from_string(row.body_html).render(**sample)
        rendered_text = env.from_string(row.body_text).render(**sample)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"template render failed: {exc}"
        )
    return EmailTemplatePreviewOut(
        subject=rendered_subject,
        body_html=rendered_html,
        body_text=rendered_text,
    )


@router.post(
    "/templates/{template_id}/reset",
    response_model=EmailTemplateOut,
    summary="Reset a template to its factory default",
)
async def reset_template(
    template_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> EmailTemplateOut:
    from backend.services.email_templates import reset_template_to_default

    row = await db.get(EmailTemplate, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="template not found")
    ok = await reset_template_to_default(db, row)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="no factory default registered for this template",
        )
    if hasattr(row, "updated_by_user_id"):
        row.updated_by_user_id = admin.id
    await db.commit()
    await db.refresh(row)
    return _template_to_out(row)


# ---------------------------------------------------------------------------
# Shared SMTP test implementation. Exported so BE-1's route signature (if
# it lands first) can forward here without duplicating transport code.
# ---------------------------------------------------------------------------


async def _smtp_test_impl(
    *,
    host: str,
    port: int,
    username,
    password,
    use_tls: bool,
    from_addr: str,
    to: str,
    timeout: float = 10.0,
):
    """Connect to host:port, post a canned message, return (ok, message).

    Failure ``message`` is a short reason string scrubbed of credentials
    / stack traces.
    """
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = to
    message["Subject"] = "Argus SMTP test"
    message.set_content(
        "This is a test email from Argus. "
        "If you received it, SMTP is configured correctly."
    )

    try:
        try:
            import aiosmtplib  # type: ignore
        except ImportError:  # pragma: no cover
            def _sync_send() -> None:
                with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                    if use_tls:
                        smtp.starttls()
                    if username and password:
                        smtp.login(username, password)
                    smtp.send_message(message)

            await asyncio.wait_for(
                asyncio.to_thread(_sync_send), timeout=timeout
            )
            return True, "sent"

        await asyncio.wait_for(
            aiosmtplib.send(
                message,
                hostname=host,
                port=port,
                username=username or None,
                password=password or None,
                start_tls=use_tls,
            ),
            timeout=timeout,
        )
        return True, "sent"
    except asyncio.TimeoutError:
        return False, f"timeout after {int(timeout)}s"
    except Exception as exc:  # noqa: BLE001
        reason = type(exc).__name__
        detail = str(exc).splitlines()[0] if str(exc) else ""
        if password and password in detail:
            detail = detail.replace(password, "***")
        if detail and len(detail) < 200:
            return False, f"{reason}: {detail}"
        return False, reason


# ===========================================================================
# BE-1 section - subscriptions / hooks / SMTP test signature
# ===========================================================================
#
# Add subscription CRUD + POST /smtp/test route signature here. The
# route handler should just forward to ``_smtp_test_impl(...)`` above.
#
# Placeholder so rebase onto BE-1's commit leaves the file coherent.


# ===========================================================================
# BE-2 section - worker observability + dead-letter + SMTP test body
# ===========================================================================


class SmtpTestIn(BaseModel):
    """Payload for POST /api/admin/email/smtp/test.

    Fields mirror persisted SMTP settings but are NOT saved - admins
    can validate unsaved values before committing.
    """
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(587, ge=1, le=65535)
    username: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=1024)
    use_tls: bool = True
    from_addr: EmailStr


class SmtpTestOut(BaseModel):
    ok: bool
    message: str | None = None
    error: str | None = None


def _smtp_host_allowlist() -> set[str] | None:
    """Read ``ARGUS_SMTP_HOST_ALLOWLIST`` and return a normalised set.

    Returns ``None`` when the env var is unset or empty (= no restriction).
    Hosts are stripped + lower-cased for case-insensitive matching.
    """
    raw = os.environ.get("ARGUS_SMTP_HOST_ALLOWLIST", "").strip()
    if not raw:
        return None
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return set(parts) if parts else None


@router.post(
    "/smtp/test",
    response_model=SmtpTestOut,
    summary="Send a test email using the supplied (unsaved) SMTP config",
)
async def smtp_test(
    body: SmtpTestIn,
    request: Request,
    admin: User = Depends(require_admin),
    locale: SupportedLocale = Depends(get_locale),
) -> SmtpTestOut:
    """Validate SMTP credentials by sending a canned test email.

    Delivery target is the admin's own address so a successful result
    lands in their inbox. Timeout 10 s; failures return
    ``{ok: false, error: "<short reason>"}`` with secrets stripped.

    Rate-limited at 10 requests/hour per admin (token bucket) and
    optionally restricted to ``ARGUS_SMTP_HOST_ALLOWLIST``. Every call
    writes one row to the audit log so abusive use is observable.
    """
    target = admin.email
    if not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="admin account has no email on file",
        )

    # Optional allowlist — reject before opening any sockets.
    allowlist = _smtp_host_allowlist()
    if allowlist is not None and body.host.strip().lower() not in allowlist:
        # Audit the rejection so admins can investigate scans.
        get_audit_service().log_background(
            action="smtp_test",
            user_id=admin.id,
            target_type="smtp",
            target_id=body.host,
            metadata={
                "port": body.port,
                "result": "error",
                "reason": "host_not_allowed",
            },
            ip=(request.client.host if request.client else None),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=tr(locale, "smtp.test.host_not_allowed", host=body.host),
        )

    # Per-admin rate limit (10/hour). Sits before the network call so a
    # locked-out admin can't keep generating outbound traffic on retry.
    bucket = get_smtp_test_bucket()
    allowed, retry_after = await bucket.try_consume(
        f"smtp-test:{admin.id}"
    )
    if not allowed:
        retry_seconds = max(1, int(retry_after + 0.999))
        get_audit_service().log_background(
            action="smtp_test",
            user_id=admin.id,
            target_type="smtp",
            target_id=body.host,
            metadata={
                "port": body.port,
                "result": "error",
                "reason": "rate_limited",
            },
            ip=(request.client.host if request.client else None),
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=tr(locale, "smtp.test.rate_limited"),
            headers={"Retry-After": str(retry_seconds)},
        )

    ok, msg = await _smtp_test_impl(
        host=body.host,
        port=body.port,
        username=body.username,
        password=body.password,
        use_tls=body.use_tls,
        from_addr=body.from_addr,
        to=target,
        timeout=10.0,
    )
    audit_ip = request.client.host if request.client else None
    if ok:
        log.info(
            "email.smtp_test.ok admin_id=%s host=%s port=%d",
            admin.id, body.host, body.port,
        )
        get_audit_service().log_background(
            action="smtp_test",
            user_id=admin.id,
            target_type="smtp",
            target_id=body.host,
            metadata={"port": body.port, "result": "ok"},
            ip=audit_ip,
        )
        return SmtpTestOut(ok=True, message=msg)
    log.warning(
        "email.smtp_test.fail admin_id=%s host=%s port=%d err=%s",
        admin.id, body.host, body.port, msg,
    )
    get_audit_service().log_background(
        action="smtp_test",
        user_id=admin.id,
        target_type="smtp",
        target_id=body.host,
        metadata={"port": body.port, "result": "error"},
        ip=audit_ip,
    )
    return SmtpTestOut(ok=False, error=msg)


# ---------------------------------------------------------------------------
# Dead letter
# ---------------------------------------------------------------------------


class DeadLetterOut(BaseModel):
    id: int
    to_addr: str
    subject: str
    event_type: str
    attempts: int
    last_error: str | None = None
    created_at: str | None = None
    status: str = "pending"


async def _fetch_dead_letter_model():
    """Lazy import so this module loads without BE-1's schema."""
    try:
        from backend.models import EmailDeadLetter  # type: ignore[attr-defined]
        return EmailDeadLetter
    except Exception:  # noqa: BLE001
        return None


@router.get("/dead-letter", summary="List dead-lettered emails")
async def list_dead_letter(
    status_filter: str = Query("pending", alias="status"),
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return up to ``limit`` dead-lettered rows matching ``status``.

    ``status`` is ``pending`` / ``retried`` / ``all``. Pre-schema the
    endpoint returns an empty list with ``schema_ready=False`` rather
    than 500-ing - keeps the admin UI usable during rollout.
    """
    model = await _fetch_dead_letter_model()
    if model is None:
        return {"items": [], "schema_ready": False}

    stmt = select(model).order_by(model.id.desc()).limit(limit)
    if status_filter != "all" and hasattr(model, "status"):
        stmt = stmt.where(model.status == status_filter)

    rows = (await db.execute(stmt)).scalars().all()
    items = [
        DeadLetterOut(
            id=r.id,
            to_addr=getattr(r, "to_address", None) or getattr(r, "to_addr", "") or "",
            subject=r.subject,
            event_type=r.event_type or "",
            attempts=int(getattr(r, "attempts", 0)),
            last_error=getattr(r, "last_error", None),
            created_at=(
                r.created_at if isinstance(getattr(r, "created_at", None), str)
                else (
                    r.created_at.isoformat()
                    if getattr(r, "created_at", None) else None
                )
            ),
            status=getattr(r, "status", "pending"),
        ).dict()
        for r in rows
    ]
    return {"items": items, "schema_ready": True}


@router.post(
    "/dead-letter/retry",
    summary="Re-enqueue one or more dead-lettered emails",
)
async def retry_dead_letter(
    ids: str = Query(..., description="Comma-separated row ids"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Pull requested rows, re-enqueue them, delete on commit.

    If a retry fails the worker re-dead-letters with a fresh row - the
    original is consumed so the UI shows clear progress.
    """
    model = await _fetch_dead_letter_model()
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="email_dead_letter table not yet available",
        )

    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ids must be comma-separated integers",
        )

    if not id_list:
        return {"requeued": 0, "missing": []}

    rows = (
        await db.execute(select(model).where(model.id.in_(id_list)))
    ).scalars().all()
    found_ids = {r.id for r in rows}
    missing = [i for i in id_list if i not in found_ids]

    for r in rows:
        # Reconstruct payload best-effort from the stored JSON column.
        try:
            import json as _json
            payload = _json.loads(getattr(r, "payload_json", None) or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        enqueue(
            EmailJob(
                to=getattr(r, "to_address", None) or getattr(r, "to_addr", "") or "",
                subject=r.subject,
                body_html=payload.get("body_html", "") or "",
                body_text=payload.get("body_text", "") or "",
                event_type=r.event_type or "",
                payload=payload,
            )
        )

    if found_ids:
        await db.execute(delete(model).where(model.id.in_(list(found_ids))))
        await db.commit()

    log.info(
        "email.deadletter.retry admin_id=%s requeued=%d missing=%d",
        admin.id, len(found_ids), len(missing),
    )
    return {"requeued": len(found_ids), "missing": missing}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get(
    "/stats",
    summary="Worker counters (queue depth + 1-hour rolling send/fail)",
)
async def email_stats(
    admin: User = Depends(require_admin),
):
    """Expose backend.services.email_worker.get_metrics()."""
    return get_metrics()
