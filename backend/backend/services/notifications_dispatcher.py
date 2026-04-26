"""Event → email dispatch hook.

This module is the seam between the domain events (watchdog flags a
batch as divergent, ingest routes record ``batch_done`` / ``batch_failed``,
share router grants a project to a user, ...) and the outbound-email
system. BE-1 owns the dispatch logic + the synchronous render/send
path; BE-2 will later swap the inline :func:`_dispatch_now` call for a
queued worker, at which point all existing call sites keep working
because they already go through :func:`dispatch_email_for_event`.

Resolution pipeline per event
-----------------------------

1. **Recipients**: batch owner + every user with an active
   :class:`BatchShare` / :class:`ProjectShare` grant. Admins are NOT
   forced in — they opt in through their own subscription row.
2. **Subscription filter**: each recipient's
   :class:`NotificationSubscription` row for (project, event_type) is
   looked up.  A concrete row wins; a null-project row is the
   per-event default; absent rows fall back to
   :data:`_DEFAULT_OPT_IN` (critical events opt in by default).
3. **Rate limit**: one same-event_type send per user per 5 minutes.
4. **Render**: the event_type's template (locale = user.preferred_locale
   with en-US fallback) rendered with a context dict containing
   ``batch``, ``job``, ``shared_by``, ``link``.
5. **Send**: via the legacy :class:`EmailService` (SMTP or stdout).
   Failures get written to :class:`EmailDeadLetter` for BE-2 to retry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from jinja2 import Environment, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.models import (
    Batch,
    BatchEmailSubscription,
    BatchShare,
    EmailDeadLetter,
    EmailTemplate,
    EmailUnsubscribeToken,
    Job,
    NotificationSubscription,
    ProjectNotificationRecipient,
    ProjectShare,
    SmtpConfig,
    User,
)
from backend.services.email_rate_limit import is_allowed as _rate_limit_allowed
from backend.services.email_worker import EmailJob, enqueue

log = logging.getLogger(__name__)


# Per-event default opt-in. Critical events (failure / divergence) default
# to opted-in so a newly-registered user gets the important emails out of
# the box; success notifications default off to avoid inbox noise.
_DEFAULT_OPT_IN: dict[str, bool] = {
    "batch_done": False,
    "batch_failed": True,
    "batch_diverged": True,
    "job_failed": True,
    "job_idle_flagged": False,
    "share_granted": True,
}

# Jinja env for rendering email_template rows.  We autoescape HTML,
# leave text bodies as-is.  The loader is unused because templates come
# from strings, not the filesystem.
_HTML_ENV = Environment(
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_TEXT_ENV = Environment(
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Unsubscribe token helper (re-used by the admin seed + render path)
# ---------------------------------------------------------------------------


async def make_unsubscribe_token(
    db: AsyncSession,
    user_id: int,
    event_type: str | None = None,
) -> str:
    """Mint and persist a fresh 32-char secret.

    Callers pass the returned string into ``{{ link.unsubscribe_url }}``
    when rendering a template. ``event_type=None`` means "unsubscribe
    from every event" and is the shape the user's one-click "turn off
    all emails" flow uses.
    """
    token = secrets.token_urlsafe(24)  # 32 chars URL-safe
    row = EmailUnsubscribeToken(
        token=token,
        user_id=user_id,
        event_type=event_type,
        created_at=_utcnow_iso(),
    )
    db.add(row)
    await db.flush()
    return token


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class DispatchOutcome:
    """Summary returned by :func:`dispatch_email_for_event` (for tests).

    ``skipped_duplicate_email`` only fires for the per-project
    recipient list (v0.1.4 multi-recipient feature) when the address
    has already received the same event via the user-subscription
    loop, so we don't double-send to a vendor whose email happens to
    match an internal user's mailbox.
    """

    sent_to: list[str] = field(default_factory=list)
    skipped_unsubscribed: list[str] = field(default_factory=list)
    skipped_rate_limited: list[str] = field(default_factory=list)
    skipped_duplicate_email: list[str] = field(default_factory=list)
    dead_lettered: list[str] = field(default_factory=list)


async def dispatch_email_for_event(
    db: AsyncSession,
    event_type: str,
    batch: Batch | None,
    job: Job | None = None,
    recipients: list[User] | None = None,
    context_extra: dict[str, Any] | None = None,
) -> DispatchOutcome:
    """Resolve recipients, filter by subscription + rate limit, send.

    Parameters
    ----------
    db:
        Active async session.  Caller commits after this returns so
        the dead-letter inserts participate in the same unit of work.
    event_type:
        One of :data:`backend.services.email_templates.SUPPORTED_EVENTS`.
    batch:
        Batch the event belongs to.  Optional for ``share_granted``
        which only ties to a project — in that case the caller passes
        ``recipients=[grantee]`` and ``context_extra``.
    job:
        Job row for ``job_*`` events.  Optional otherwise.
    recipients:
        Pre-resolved user list.  When ``None`` and ``batch`` is given
        we derive recipients from (owner + share grants).
    context_extra:
        Extra keys merged into the Jinja render context.  Used by
        ``share_granted`` to pass ``shared_by`` / ``project`` /
        ``permission``.
    """
    outcome = DispatchOutcome()
    settings = get_settings()

    # Derive recipients when the caller didn't pre-resolve them.
    if recipients is None:
        if batch is None:
            log.debug(
                "dispatch: no recipients and no batch; nothing to do"
            )
            return outcome
        recipients = list(await _resolve_batch_recipients(db, batch))

    if not recipients:
        return outcome

    project = (batch.project if batch is not None else None) or (
        (context_extra or {}).get("project")
    )

    for user in recipients:
        # Subscription check.  The per-batch override (if any) wins;
        # absent / disabled override → fall through to project-level
        # logic so existing subscriptions keep working unchanged.
        if not await _is_subscribed(
            db, user.id, project, event_type,
            batch_id=batch.id if batch is not None else None,
        ):
            outcome.skipped_unsubscribed.append(user.email)
            continue

        # Rate-limit check — delegated to the shared
        # :mod:`email_rate_limit` bucket so BE-2's worker and the hook
        # layer use the same sliding window.
        if not await _rate_limit_allowed(user.id, event_type):
            outcome.skipped_rate_limited.append(user.email)
            continue

        # Template lookup
        template = await _pick_template(db, event_type, user.preferred_locale)
        if template is None:
            log.warning(
                "dispatch: no template for event_type=%s locale=%s",
                event_type,
                user.preferred_locale,
            )
            continue

        # Render
        try:
            unsub = await make_unsubscribe_token(
                db, user.id, event_type=event_type
            )
            context = _build_context(
                batch=batch,
                job=job,
                user=user,
                unsubscribe_token=unsub,
                base_url=settings.base_url,
                extra=context_extra,
            )
            subject = _HTML_ENV.from_string(template.subject).render(**context)
            body_html = _HTML_ENV.from_string(template.body_html).render(
                **context
            )
            body_text = _TEXT_ENV.from_string(template.body_text).render(
                **context
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "dispatch: render failed for user=%s event=%s: %r",
                user.id,
                event_type,
                exc,
            )
            await _record_dead_letter(
                db,
                to=user.email,
                subject=f"[render-failed] {event_type}",
                event_type=event_type,
                payload={"error": str(exc)},
                last_error=str(exc),
            )
            outcome.dead_lettered.append(user.email)
            continue

        # Fire-and-forget onto BE-2's worker queue.  Any transport
        # failure is handled by the worker (retry + dead-letter).
        enqueue(
            EmailJob(
                to=user.email,
                subject=subject,
                body_html=body_html,
                body_text=body_text,
                event_type=event_type,
                payload={
                    "user_id": user.id,
                    "batch_id": batch.id if batch is not None else None,
                    "job_id": job.id if job is not None else None,
                },
            )
        )
        outcome.sent_to.append(user.email)

    # ---------------------------------------------------------------
    # Per-project recipient list (v0.1.4 multi-recipient feature)
    # ---------------------------------------------------------------
    # An external recipient (vendor, shared-team alias, …) may be
    # subscribed to a project via :class:`ProjectNotificationRecipient`
    # without owning an Argus account.  We dispatch to those rows
    # AFTER the user-subscription loop so the user-level send takes
    # precedence (one email per address).  Dedup keys on ``email``
    # rather than ``user.id`` because the recipient might be an
    # external address that happens to match an internal user's mail.
    #
    # ``share_granted`` contract note
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # The fan-out below filters by ``event_type in r.event_kinds``,
    # which means a row whose ``event_kinds`` includes
    # ``share_granted`` would receive grant emails for every share on
    # the project — almost never what an external recipient (vendor,
    # shared-alias inbox) actually wants. We rely on the frontend
    # ``EVENT_KINDS`` whitelist in
    # ``frontend/src/components/notifications/ProjectRecipientsPanel.vue``
    # (lines ~46-54) to keep ``share_granted`` out of the picker, so
    # an admin cannot subscribe an external address to it through the
    # UI.  The backend does not enforce this — direct API callers can
    # still set the kind — but the contract is "share-granted is a
    # user-account event, not a project recipient event".  Revisit
    # both layers together if that ever needs to change.
    if batch is not None:
        already_sent: set[str] = {addr.lower() for addr in outcome.sent_to}
        recipient_rows = (
            await db.execute(
                select(ProjectNotificationRecipient)
                .where(
                    ProjectNotificationRecipient.project == batch.project
                )
                .where(ProjectNotificationRecipient.enabled.is_(True))
            )
        ).scalars().all()

        for r in recipient_rows:
            email_lc = (r.email or "").lower()
            if not email_lc:
                continue
            if email_lc in already_sent:
                outcome.skipped_duplicate_email.append(r.email)
                continue
            kinds = _decode_event_kinds(r.event_kinds)
            if event_type not in kinds:
                outcome.skipped_unsubscribed.append(r.email)
                continue

            # Recipient-side rate limit. The bucket is keyed on
            # negative ids so user-id collisions are impossible. Use
            # SHA-256 instead of Python's built-in ``hash()`` so the
            # bucket survives worker restart — ``hash()`` is salted per
            # process (PYTHONHASHSEED), which previously meant a
            # restart silently reset every recipient's rate-limit
            # window.
            rl_key = -int.from_bytes(
                hashlib.sha256(email_lc.encode("utf-8")).digest()[:8],
                "big",
            )
            if not await _rate_limit_allowed(rl_key, event_type):
                outcome.skipped_rate_limited.append(r.email)
                continue

            # Recipients have no preferred_locale; default to en-US so
            # the lookup picks the canonical English template.
            template = await _pick_template(db, event_type, "en-US")
            if template is None:
                log.warning(
                    "dispatch: no template for event_type=%s "
                    "(recipient %s)",
                    event_type,
                    r.email,
                )
                continue

            try:
                context = _build_recipient_context(
                    batch=batch,
                    job=job,
                    recipient_email=r.email,
                    unsubscribe_token=r.unsubscribe_token,
                    base_url=settings.base_url,
                    extra=context_extra,
                )
                subject = _HTML_ENV.from_string(template.subject).render(
                    **context
                )
                body_html = _HTML_ENV.from_string(template.body_html).render(
                    **context
                )
                body_text = _TEXT_ENV.from_string(template.body_text).render(
                    **context
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "dispatch: render failed for recipient=%s "
                    "event=%s: %r",
                    r.email,
                    event_type,
                    exc,
                )
                await _record_dead_letter(
                    db,
                    to=r.email,
                    subject=f"[render-failed] {event_type}",
                    event_type=event_type,
                    payload={"error": str(exc)},
                    last_error=str(exc),
                )
                outcome.dead_lettered.append(r.email)
                continue

            enqueue(
                EmailJob(
                    to=r.email,
                    subject=subject,
                    body_html=body_html,
                    body_text=body_text,
                    event_type=event_type,
                    payload={
                        "recipient_id": r.id,
                        "project": r.project,
                        "batch_id": batch.id,
                        "job_id": job.id if job is not None else None,
                    },
                )
            )
            outcome.sent_to.append(r.email)
            already_sent.add(email_lc)

    return outcome


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------


async def _resolve_batch_recipients(
    db: AsyncSession, batch: Batch
) -> Iterable[User]:
    """Owner + every grantee (batch-share + project-share) for the batch."""
    seen: set[int] = set()
    result: list[User] = []

    if batch.owner_id is not None:
        owner = await db.get(User, batch.owner_id)
        if owner is not None and owner.is_active:
            seen.add(owner.id)
            result.append(owner)

    # Batch-level shares
    rows = (
        await db.execute(
            select(User)
            .join(BatchShare, BatchShare.grantee_id == User.id)
            .where(BatchShare.batch_id == batch.id)
        )
    ).scalars().all()
    for u in rows:
        if u.id in seen or not u.is_active:
            continue
        seen.add(u.id)
        result.append(u)

    # Project-level shares — match (owner_id, project)
    if batch.owner_id is not None:
        rows = (
            await db.execute(
                select(User)
                .join(ProjectShare, ProjectShare.grantee_id == User.id)
                .where(ProjectShare.owner_id == batch.owner_id)
                .where(ProjectShare.project == batch.project)
            )
        ).scalars().all()
        for u in rows:
            if u.id in seen or not u.is_active:
                continue
            seen.add(u.id)
            result.append(u)

    return result


async def _is_subscribed(
    db: AsyncSession,
    user_id: int,
    project: str | None,
    event_type: str,
    batch_id: str | None = None,
) -> bool:
    """Return the effective subscription for ``(user, project, event_type)``.

    Resolution order:
      0. **Per-batch override** (if ``batch_id`` is given): when a row
         exists in ``batch_email_subscription`` for ``(user, batch)``
         AND it is enabled, the override decides — present in
         ``event_kinds`` → send, absent → skip.  A disabled override
         row is treated as "no override" (caller asked to keep the
         row but defer to project-level logic).
      1. Specific-project row in ``notification_subscription``.
      2. Global-default row (``project IS NULL``).
      3. Hard-coded :data:`_DEFAULT_OPT_IN`.

    Per-batch overrides only apply to the batch owner today (the API
    refuses non-owner writes), but the lookup is owner-agnostic — a
    project-share recipient who happens to also own the batch still
    benefits from their own override.
    """
    # 0. Per-batch override
    if batch_id is not None:
        override = await db.get(BatchEmailSubscription, (user_id, batch_id))
        if override is not None and bool(override.enabled):
            kinds = _decode_event_kinds(override.event_kinds)
            return event_type in kinds

    # 1. Specific-project row
    if project is not None:
        row = (
            await db.execute(
                select(NotificationSubscription)
                .where(NotificationSubscription.user_id == user_id)
                .where(NotificationSubscription.project == project)
                .where(NotificationSubscription.event_type == event_type)
            )
        ).scalar_one_or_none()
        if row is not None:
            return bool(row.enabled)

    # 2. Global default row
    row = (
        await db.execute(
            select(NotificationSubscription)
            .where(NotificationSubscription.user_id == user_id)
            .where(NotificationSubscription.project.is_(None))
            .where(NotificationSubscription.event_type == event_type)
        )
    ).scalar_one_or_none()
    if row is not None:
        return bool(row.enabled)

    # 3. Hard-coded default
    return _DEFAULT_OPT_IN.get(event_type, True)


def _decode_event_kinds(raw: str | None) -> list[str]:
    """Tolerant JSON-list decoder for ``BatchEmailSubscription.event_kinds``.

    A malformed / null value is treated as an empty list so a corrupt
    row can't make the dispatcher crash on every send.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if isinstance(x, str)]


# ---------------------------------------------------------------------------
# Template lookup + render
# ---------------------------------------------------------------------------


async def _pick_template(
    db: AsyncSession, event_type: str, locale: str | None
) -> EmailTemplate | None:
    """Return the most specific template for (event_type, locale).

    Falls back to ``en-US`` when the requested locale is missing so a
    Chinese user configured with ``preferred_locale='fr-FR'`` still
    receives English mail rather than no mail.
    """
    locale = locale or "en-US"
    row = (
        await db.execute(
            select(EmailTemplate)
            .where(EmailTemplate.event_type == event_type)
            .where(EmailTemplate.locale == locale)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    return (
        await db.execute(
            select(EmailTemplate)
            .where(EmailTemplate.event_type == event_type)
            .where(EmailTemplate.locale == "en-US")
        )
    ).scalar_one_or_none()


def _build_context(
    *,
    batch: Batch | None,
    job: Job | None,
    user: User,
    unsubscribe_token: str,
    base_url: str,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """Materialise the dict handed to Jinja for one recipient."""
    batch_dict: dict[str, Any] = {}
    if batch is not None:
        batch_dict = {
            "id": batch.id,
            "name": batch.name or batch.id,
            "project": batch.project,
            "status": batch.status,
            "start_time": batch.start_time,
            "end_time": batch.end_time,
            "host": batch.host,
        }
    job_dict: dict[str, Any] = {}
    if job is not None:
        err: str | None = None
        if job.extra:
            try:
                payload = json.loads(job.extra)
                err = payload.get("error") or payload.get("last_error")
            except Exception:  # noqa: BLE001
                err = None
        job_dict = {
            "id": job.id,
            "model": job.model,
            "dataset": job.dataset,
            "status": job.status,
            "error": err,
        }
    base = base_url.rstrip("/")
    link = {
        "batch_url": (
            f"{base}/batches/{batch.id}" if batch is not None else base
        ),
        "project_url": (
            f"{base}/projects/{batch.project}"
            if batch is not None and batch.project
            else base
        ),
        "unsubscribe_url": f"{base}/api/unsubscribe?token={unsubscribe_token}",
    }
    context: dict[str, Any] = {
        "batch": batch_dict,
        "job": job_dict,
        "link": link,
        "user": {"id": user.id, "username": user.username, "email": user.email},
    }
    if extra:
        context.update(extra)
    return context


def _build_recipient_context(
    *,
    batch: Batch,
    job: Job | None,
    recipient_email: str,
    unsubscribe_token: str,
    base_url: str,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """Render context for a :class:`ProjectNotificationRecipient` send.

    Same shape as :func:`_build_context` but the ``user`` slot
    carries the external address so existing templates referencing
    ``{{ user.email }}`` keep working, and the ``unsubscribe_url``
    points at the recipient-flavoured public endpoint instead of the
    user-flavoured ``/api/unsubscribe?token=...``.
    """
    base = base_url.rstrip("/")
    batch_dict = {
        "id": batch.id,
        "name": batch.name or batch.id,
        "project": batch.project,
        "status": batch.status,
        "start_time": batch.start_time,
        "end_time": batch.end_time,
        "host": batch.host,
    }
    job_dict: dict[str, Any] = {}
    if job is not None:
        err: str | None = None
        if job.extra:
            try:
                payload = json.loads(job.extra)
                err = payload.get("error") or payload.get("last_error")
            except Exception:  # noqa: BLE001
                err = None
        job_dict = {
            "id": job.id,
            "model": job.model,
            "dataset": job.dataset,
            "status": job.status,
            "error": err,
        }
    link = {
        "batch_url": f"{base}/batches/{batch.id}",
        "project_url": (
            f"{base}/projects/{batch.project}" if batch.project else base
        ),
        # Public token endpoint mounted by
        # ``project_notification_recipients.unsubscribe_router``.
        "unsubscribe_url": (
            f"{base}/api/unsubscribe/recipient/{unsubscribe_token}"
        ),
    }
    context: dict[str, Any] = {
        "batch": batch_dict,
        "job": job_dict,
        "link": link,
        # ``user.email`` slot reused so existing templates render the
        # recipient's address; ``id`` and ``username`` are absent so
        # any ``{{ user.username }}`` falls back to the empty string.
        "user": {"id": None, "username": "", "email": recipient_email},
    }
    if extra:
        context.update(extra)
    return context


# ---------------------------------------------------------------------------
# Sending + dead-lettering
# ---------------------------------------------------------------------------


async def _record_dead_letter(
    db: AsyncSession,
    *,
    to: str,
    subject: str,
    event_type: str,
    payload: dict[str, Any] | None,
    last_error: str,
) -> None:
    """Insert an :class:`EmailDeadLetter` row for BE-2 to retry."""
    row = EmailDeadLetter(
        to_address=to,
        subject=subject[:500],
        event_type=event_type,
        payload_json=(
            json.dumps(payload, default=str) if payload is not None else None
        ),
        attempts=0,
        last_error=last_error[:2000],
        created_at=_utcnow_iso(),
    )
    db.add(row)
    await db.flush()


# ---------------------------------------------------------------------------
# Test-only helpers
# ---------------------------------------------------------------------------


def reset_rate_limit_for_tests() -> None:
    """Clear the shared email rate-limit bucket (delegated to
    :mod:`backend.services.email_rate_limit`)."""
    from backend.services.email_rate_limit import (
        reset_email_bucket_for_tests,
    )
    reset_email_bucket_for_tests()


# ---------------------------------------------------------------------------
# SMTP-config fetch (used by admin test-send + future worker)
# ---------------------------------------------------------------------------


async def load_smtp_config(db: AsyncSession) -> SmtpConfig | None:
    """Return the single-row SMTP config (or ``None`` if unset)."""
    return await db.get(SmtpConfig, 1)
