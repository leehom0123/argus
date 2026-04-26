"""Async email service.

Two behaviours, driven by config:

1. SMTP configured → open an ``aiosmtplib`` connection and send. Failures
   are logged at ERROR level and *not* propagated, so callers can
   fire-and-forget.

2. SMTP not configured → render the template, log an INFO line plus the
   rendered HTML to stdout. This makes local dev productive without having
   to stand up a mail server, and is the behaviour requirements §4.5 calls
   out as acceptable for the MVP.

The class keeps a tiny stub counter so tests can assert "did we try to send
the verify email?" without monkeypatching SMTP.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from backend.config import Settings, get_settings
from backend.i18n import tr

log = logging.getLogger(__name__)


BACKEND_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BACKEND_DIR / "emails" / "templates"


def _build_env(search_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(search_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


@dataclass
class SentMessage:
    to: str
    subject: str
    body_html: str
    template: str
    context: dict[str, Any]


class EmailService:
    """Render Jinja2 templates and send (or fall back to stdout)."""

    def __init__(
        self,
        settings: Settings | None = None,
        templates_dir: Path = TEMPLATES_DIR,
    ) -> None:
        self._settings = settings or get_settings()
        self._env = _build_env(templates_dir)
        # Used by tests to assert on sent mail without a real SMTP server.
        self.sent_messages: list[SentMessage] = field(default_factory=list)  # type: ignore[assignment]
        self.sent_messages = []

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    # Supported locales for bilingual templates. Any value not in this set
    # falls back to "en-US".
    _SUPPORTED_LOCALES: frozenset[str] = frozenset({"en-US", "zh-CN"})

    _SUBJECTS: dict[str, dict[str, str]] = {
        "verify": {
            "en-US": "Verify your Argus email",
            "zh-CN": "请验证您的 Argus 邮箱",
        },
        "reset_password": {
            "en-US": "Reset your Argus password",
            "zh-CN": "重置您的 Argus 密码",
        },
    }

    def _resolve_locale(self, locale: str) -> str:
        """Return *locale* if supported, else ``"en-US"``."""
        return locale if locale in self._SUPPORTED_LOCALES else "en-US"

    def _template_name(self, base: str, locale: str) -> str:
        """Return ``"{base}.{locale}.html"`` when it exists, else ``"{base}.en-US.html"``."""
        resolved = self._resolve_locale(locale)
        candidate = f"{base}.{resolved}.html"
        # If the locale-specific template is missing for any reason, fall back to en-US.
        try:
            self._env.get_template(candidate)
            return candidate
        except Exception:  # noqa: BLE001
            return f"{base}.en-US.html"

    async def send_verification(
        self,
        to: str,
        verify_url: str,
        username: str,
        *,
        locale: str = "en-US",
    ) -> bool:
        resolved = self._resolve_locale(locale)
        subject = self._SUBJECTS["verify"].get(
            resolved, self._SUBJECTS["verify"]["en-US"]
        )
        return await self._send(
            to=to,
            subject=subject,
            template=self._template_name("verify", locale),
            context={
                "username": username,
                "verify_url": verify_url,
                "ttl_hours": self._settings.email_verify_ttl_hours,
                "base_url": self._settings.base_url,
            },
        )

    async def send_anomalous_login(
        self,
        to: str,
        username: str,
        *,
        ip: str,
        user_agent: str,
        when_iso: str,
        locale: str = "en-US",
    ) -> bool:
        """Notify a user that a login happened from a new (ip, UA).

        Rendered inline rather than through a Jinja template because the
        body only needs four translated lines; keeping it in-code avoids
        shipping six more template files for what is effectively a
        plain-text notice. ``tr()`` handles locale fallback — unknown
        locales silently degrade to en-US.
        """
        resolved = self._resolve_locale(locale)
        subject = tr(resolved, "auth.anomalous_login.subject")
        intro = tr(resolved, "auth.anomalous_login.intro")
        lbl_ip = tr(resolved, "auth.anomalous_login.ip")
        lbl_ua = tr(resolved, "auth.anomalous_login.user_agent")
        lbl_when = tr(resolved, "auth.anomalous_login.when")
        not_you = tr(resolved, "auth.anomalous_login.not_you")
        from html import escape as _esc
        html = (
            f"<p>Hello {_esc(username)},</p>"
            f"<p>{_esc(intro)}</p>"
            f"<ul>"
            f"<li><b>{_esc(lbl_ip)}:</b> {_esc(ip)}</li>"
            f"<li><b>{_esc(lbl_ua)}:</b> {_esc(user_agent)}</li>"
            f"<li><b>{_esc(lbl_when)}:</b> {_esc(when_iso)}</li>"
            f"</ul>"
            f"<p>{_esc(not_you)}</p>"
        )
        record = SentMessage(
            to=to,
            subject=subject,
            body_html=html,
            template="<anomalous_login-inline>",
            context={
                "username": username,
                "ip": ip,
                "user_agent": user_agent,
                "when_iso": when_iso,
                "locale": resolved,
            },
        )
        self.sent_messages.append(record)

        if not self._settings.smtp_configured:
            log.warning(
                "SMTP not configured; anomalous-login email to %s "
                "would have been sent.",
                to,
            )
            log.info(
                "[email-dev-stdout] to=%s subject=%r inline-template\n%s",
                to,
                subject,
                html,
            )
            return True

        message = EmailMessage()
        message["From"] = self._settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.add_alternative(html, subtype="html")
        try:
            try:
                import aiosmtplib  # type: ignore
            except ImportError:  # pragma: no cover
                await asyncio.to_thread(self._send_smtplib_sync, message)
                return True
            await aiosmtplib.send(
                message,
                hostname=self._settings.smtp_host,
                port=self._settings.smtp_port,
                username=self._settings.smtp_user,
                password=self._settings.smtp_pass,
                start_tls=self._settings.smtp_use_tls,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error(
                "failed to send anomalous-login email to %s: %s", to, exc
            )
            return False

    async def send_password_changed_notification(
        self,
        to: str,
        *,
        locale: str = "en-US",
        ip: str = "unknown",
        user_agent: str = "unknown",
    ) -> bool:
        """Fire an "your password just changed" notice to the user.

        Sent from the ``POST /api/auth/change-password`` handler as an
        out-of-band confirmation: if the change wasn't initiated by the
        real account owner (e.g. JWT got stolen) the email is their
        second signal to act. Rendered inline — no Jinja template file
        needed, mirroring ``send_anomalous_login``.
        """
        resolved = self._resolve_locale(locale)
        subject = tr(resolved, "auth.password.changed_subject")
        body = tr(
            resolved,
            "auth.password.changed_body",
            ip=ip,
            user_agent=user_agent,
        )
        from html import escape as _esc
        html = (
            f"<p>Hello,</p>"
            f"<p>{_esc(body)}</p>"
        )
        record = SentMessage(
            to=to,
            subject=subject,
            body_html=html,
            template="<password_changed-inline>",
            context={
                "ip": ip,
                "user_agent": user_agent,
                "locale": resolved,
            },
        )
        self.sent_messages.append(record)

        if not self._settings.smtp_configured:
            log.warning(
                "SMTP not configured; password-changed email to %s "
                "would have been sent.",
                to,
            )
            log.info(
                "[email-dev-stdout] to=%s subject=%r inline-template\n%s",
                to,
                subject,
                html,
            )
            return True

        message = EmailMessage()
        message["From"] = self._settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.add_alternative(html, subtype="html")
        try:
            try:
                import aiosmtplib  # type: ignore
            except ImportError:  # pragma: no cover
                await asyncio.to_thread(self._send_smtplib_sync, message)
                return True
            await aiosmtplib.send(
                message,
                hostname=self._settings.smtp_host,
                port=self._settings.smtp_port,
                username=self._settings.smtp_user,
                password=self._settings.smtp_pass,
                start_tls=self._settings.smtp_use_tls,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error(
                "failed to send password-changed email to %s: %s", to, exc
            )
            return False

    async def send_email_change_verification(
        self,
        to: str,
        verify_url: str,
        username: str,
        *,
        locale: str = "en-US",
    ) -> bool:
        """Mail a confirm-link to the **new** email for the email-change flow.

        Rendered inline (no Jinja template file) to keep the schema delta
        small — the body is just a one-line "click to confirm" plus the
        link, mirroring ``send_password_changed_notification`` style.
        """
        resolved = self._resolve_locale(locale)
        subject = tr(resolved, "auth.email.change_subject")
        body = tr(resolved, "auth.email.change_body")
        from html import escape as _esc
        html = (
            f"<p>Hello {_esc(username)},</p>"
            f"<p>{_esc(body)}</p>"
            f"<p><a href=\"{_esc(verify_url)}\">{_esc(verify_url)}</a></p>"
        )
        record = SentMessage(
            to=to,
            subject=subject,
            body_html=html,
            template="<email_change-inline>",
            context={
                "username": username,
                "verify_url": verify_url,
                "locale": resolved,
            },
        )
        self.sent_messages.append(record)

        if not self._settings.smtp_configured:
            log.warning(
                "SMTP not configured; email-change verification to %s "
                "would have been sent.",
                to,
            )
            log.info(
                "[email-dev-stdout] to=%s subject=%r inline-template\n%s",
                to,
                subject,
                html,
            )
            return True

        message = EmailMessage()
        message["From"] = self._settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.add_alternative(html, subtype="html")
        try:
            try:
                import aiosmtplib  # type: ignore
            except ImportError:  # pragma: no cover
                await asyncio.to_thread(self._send_smtplib_sync, message)
                return True
            await aiosmtplib.send(
                message,
                hostname=self._settings.smtp_host,
                port=self._settings.smtp_port,
                username=self._settings.smtp_user,
                password=self._settings.smtp_pass,
                start_tls=self._settings.smtp_use_tls,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error(
                "failed to send email-change verification to %s: %s", to, exc
            )
            return False

    async def send_password_reset(
        self,
        to: str,
        reset_url: str,
        username: str,
        *,
        locale: str = "en-US",
    ) -> bool:
        resolved = self._resolve_locale(locale)
        subject = self._SUBJECTS["reset_password"].get(
            resolved, self._SUBJECTS["reset_password"]["en-US"]
        )
        return await self._send(
            to=to,
            subject=subject,
            template=self._template_name("reset_password", locale),
            context={
                "username": username,
                "reset_url": reset_url,
                "ttl_minutes": self._settings.password_reset_ttl_minutes,
                "base_url": self._settings.base_url,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _send(
        self,
        *,
        to: str,
        subject: str,
        template: str,
        context: dict[str, Any],
    ) -> bool:
        html = self._env.get_template(template).render(**context)
        record = SentMessage(
            to=to,
            subject=subject,
            body_html=html,
            template=template,
            context=dict(context),
        )
        self.sent_messages.append(record)

        if not self._settings.smtp_configured:
            # Dev fallback. Print the whole message to stdout via logger so
            # it's captured by the test harness and visible in docker logs.
            log.warning(
                "SMTP not configured; email to %s would have been sent.",
                to,
            )
            log.info(
                "[email-dev-stdout] to=%s subject=%r template=%s\n%s",
                to,
                subject,
                template,
                html,
            )
            return True

        message = EmailMessage()
        message["From"] = self._settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.add_alternative(html, subtype="html")

        try:
            # aiosmtplib is an optional import path; fall back to the stdlib
            # smtplib running in a thread if the optional dep is missing.
            try:
                import aiosmtplib  # type: ignore
            except ImportError:  # pragma: no cover
                await asyncio.to_thread(self._send_smtplib_sync, message)
                return True

            await aiosmtplib.send(
                message,
                hostname=self._settings.smtp_host,
                port=self._settings.smtp_port,
                username=self._settings.smtp_user,
                password=self._settings.smtp_pass,
                start_tls=self._settings.smtp_use_tls,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            # Fire-and-forget per requirements §4.5: never surface SMTP
            # errors to the HTTP response. Audit logs are a phase-2 thing.
            log.error(
                "failed to send email to %s (template=%s): %s",
                to,
                template,
                exc,
            )
            return False

    def _send_smtplib_sync(self, message: EmailMessage) -> None:
        """Blocking fallback when ``aiosmtplib`` is unavailable."""
        assert self._settings.smtp_host  # guarded by caller
        with smtplib.SMTP(
            self._settings.smtp_host, self._settings.smtp_port
        ) as smtp:
            if self._settings.smtp_use_tls:
                smtp.starttls()
            if self._settings.smtp_user and self._settings.smtp_pass:
                smtp.login(self._settings.smtp_user, self._settings.smtp_pass)
            smtp.send_message(message)


# ---------------------------------------------------------------------------
# Singleton accessor — the FastAPI Depends layer wraps this.
# ---------------------------------------------------------------------------


_instance: EmailService | None = None


def get_email_service() -> EmailService:
    global _instance
    if _instance is None:
        _instance = EmailService()
    return _instance


def reset_email_service_for_tests() -> None:
    """Clear the module-level cached instance (used by pytest fixtures)."""
    global _instance
    _instance = None
