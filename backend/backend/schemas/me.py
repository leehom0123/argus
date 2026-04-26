"""Pydantic DTOs for the ``/api/me/*`` user-self endpoints (#108).

These cover the per-user surfaces the Settings UI talks to that don't
fit cleanly under ``/api/auth/*`` (auth flow) or
``/api/me/subscriptions`` (per-(project, event_type) matrix).

* :class:`NotificationPrefsOut` / :class:`NotificationPrefsIn` — the
  five toggle defaults that the user can flip from
  Settings → Notifications. They are *defaults* for new batches; the
  per-batch ``batch_email_subscription`` row, when present, always
  takes precedence at dispatch time.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class NotificationPrefsOut(BaseModel):
    """Read-side projection of ``user.notification_prefs_json``.

    Even when the user has never PUT prefs (column is NULL) we still
    return a fully-populated dict — the canonical defaults — so the
    frontend never has to special-case ``null`` and the persistence
    state is invisible to it.
    """

    model_config = ConfigDict(extra="forbid")

    notify_batch_done: bool
    notify_batch_failed: bool
    notify_job_failed: bool
    notify_diverged: bool
    notify_job_idle: bool


class NotificationPrefsIn(BaseModel):
    """Write-side: the body of ``PUT /api/me/notification_prefs``.

    All five keys are required so the API has total-update semantics —
    avoids the "did the client mean to leave this off?" ambiguity that
    a PATCH would introduce. Round-tripping a GET → PUT works exactly.
    """

    model_config = ConfigDict(extra="forbid")

    notify_batch_done: bool
    notify_batch_failed: bool
    notify_job_failed: bool
    notify_diverged: bool
    notify_job_idle: bool
