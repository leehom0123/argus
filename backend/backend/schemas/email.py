"""Pydantic DTOs for the ``/api/admin/email/*`` + ``/api/me/subscriptions``
endpoints.

The password field on :class:`SmtpConfigOut` / :class:`SmtpConfigIn` is
always masked as ``"***"`` on the way out.  On PUT, a value of ``"***"``
is treated as a sentinel meaning "keep the existing password" so the
admin UI can round-trip a read without clobbering the secret.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# Sentinel the admin endpoint recognises as "preserve the stored password".
MASKED_PASSWORD: str = "***"


class SmtpConfigOut(BaseModel):
    """SMTP config projection returned by GET /api/admin/email/smtp."""

    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    # Always masked; real secret never leaves the server.
    smtp_password: str = Field(default=MASKED_PASSWORD)
    smtp_from_address: str | None = None
    smtp_from_name: str | None = None
    use_tls: bool = True
    use_ssl: bool = False
    updated_at: str | None = None
    updated_by_user_id: int | None = None


class SmtpConfigIn(BaseModel):
    """Body for PUT /api/admin/email/smtp.

    A literal ``"***"`` in ``smtp_password`` means "don't overwrite the
    stored password"; any other value (including the empty string)
    replaces it.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str = Field(default=MASKED_PASSWORD)
    smtp_from_address: str | None = None
    smtp_from_name: str | None = None
    use_tls: bool = True
    use_ssl: bool = False


class SmtpTestResult(BaseModel):
    """Outcome of POST /api/admin/email/smtp/test."""

    ok: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class EmailTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    locale: str
    subject: str
    body_html: str
    body_text: str
    is_system: bool
    updated_at: str | None = None
    updated_by_user_id: int | None = None


class EmailTemplateUpdateIn(BaseModel):
    """Subject / body edits for an existing template.

    ``event_type`` and ``locale`` are immutable — changing them would
    let an admin accidentally overwrite a sibling row's identity.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str
    body_html: str
    body_text: str


class EmailTemplatePreviewOut(BaseModel):
    subject: str
    body_html: str
    body_text: str


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class SubscriptionRow(BaseModel):
    """One notification subscription row.

    ``project`` may be null to express "global default for this
    event_type".  Per-project rows shadow the global default.
    """

    model_config = ConfigDict(from_attributes=True)

    project: str | None = None
    event_type: str
    enabled: bool


class SubscriptionBulkIn(BaseModel):
    """Bulk upsert body for PATCH /api/me/subscriptions."""

    model_config = ConfigDict(extra="forbid")

    subscriptions: list[SubscriptionRow] = Field(default_factory=list)


class UnsubscribeResult(BaseModel):
    ok: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# Per-batch subscription override
# ---------------------------------------------------------------------------


class BatchEmailSubscriptionOut(BaseModel):
    """One :class:`BatchEmailSubscription` row.

    ``event_kinds`` is the parsed JSON list (already a Python ``list[str]``);
    the wire format hides the on-disk JSON encoding from the UI.
    """

    model_config = ConfigDict(from_attributes=True)

    batch_id: str
    event_kinds: list[str] = Field(default_factory=list)
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None


class BatchEmailSubscriptionIn(BaseModel):
    """Body for PUT /api/batches/{batch_id}/email-subscription."""

    model_config = ConfigDict(extra="forbid")

    event_kinds: list[str] = Field(default_factory=list)
    enabled: bool = True


# ---------------------------------------------------------------------------
# Per-project recipient list (multi-recipient notifications, v0.1.4)
# ---------------------------------------------------------------------------


class ProjectRecipientOut(BaseModel):
    """One :class:`ProjectNotificationRecipient` row.

    ``event_kinds`` is the parsed JSON list (already ``list[str]``);
    the wire format hides the on-disk JSON encoding from the UI.
    ``unsubscribe_token`` is intentionally NOT exposed here — only the
    backend embeds it in outgoing email footers, never returned to
    authenticated callers.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project: str
    email: str
    event_kinds: list[str] = Field(default_factory=list)
    enabled: bool = True
    added_by_user_id: int
    created_at: str | None = None
    updated_at: str | None = None


class ProjectRecipientIn(BaseModel):
    """Body for ``POST /api/projects/{project}/recipients``.

    ``email`` is validated as RFC 5322 via Pydantic's :class:`EmailStr`
    so a UI typo can't smuggle ``"hello"`` into the recipient list.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    event_kinds: list[str] = Field(default_factory=list)
    enabled: bool = True


class ProjectRecipientPatchIn(BaseModel):
    """Body for ``PATCH /api/projects/{project}/recipients/{id}``.

    Every field is optional — callers PATCH only what they want to
    change.  ``email`` may be updated (e.g. correcting a typo) and
    re-validates as :class:`EmailStr`; the API enforces UNIQUE on
    ``(project, email)`` so a collision returns 409.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = None
    event_kinds: list[str] | None = None
    enabled: bool | None = None
