"""Audit log writer.

A thin helper wrapping ``INSERT INTO audit_log`` so every caller uses
the same shape. Intentionally tolerant of failures — losing an audit
row should not take down the primary operation (login, share, etc.).

Two entry points:

* :func:`AuditService.log` — awaits the write inline. Use when the
  calling endpoint already owns an open session and the audit is
  cheap. Falls back to a fresh session if none supplied.
* :func:`AuditService.log_background` — schedules the write as an
  ``asyncio.create_task`` with a ref-kept handle so Python won't GC
  the coroutine mid-flight (the M5 carryover fix uses the same
  technique for notification dispatch).

Both routes ultimately funnel through :func:`_write_row`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import SessionLocal
from backend.models import AuditLog

log = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    """Return current UTC as ISO 8601 ending in ``Z``."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# Set keeping a strong reference to in-flight background tasks. Without
# this, Python's GC can reap the task before it runs (PEP-3148 footnote,
# same pattern as the notification dispatch). We `discard` each task in
# its own done_callback to avoid unbounded growth.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _log_task_exception(task: asyncio.Task) -> None:
    """Log any exception raised by a background task, then drop the ref."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        exc = None
    if exc is not None:
        log.warning("audit background task failed: %r", exc)
    _BACKGROUND_TASKS.discard(task)


class AuditService:
    """Write rows into the ``audit_log`` table."""

    async def log(
        self,
        *,
        action: str,
        user_id: int | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ip: str | None = None,
        db: AsyncSession | None = None,
    ) -> None:
        """Insert one :class:`AuditLog` row.

        Parameters
        ----------
        action:
            Short identifier (``'login_success'`` etc). Required.
        user_id:
            Acting user; ``None`` for anonymous events.
        target_type / target_id:
            Entity affected by the action, if any.
        metadata:
            JSON-serialisable payload with extra context.
        ip:
            Client IP (best-effort; may be proxied).
        db:
            Optional active session. If omitted we open a fresh one so
            the audit write doesn't entangle with the caller's txn.
        """
        try:
            if db is not None:
                await _write_row(
                    db,
                    action=action,
                    user_id=user_id,
                    target_type=target_type,
                    target_id=target_id,
                    metadata=metadata,
                    ip=ip,
                )
                # Flush only — commit remains the caller's call so the
                # audit row participates in the same unit of work.
                await db.flush()
            else:
                async with SessionLocal() as fresh:
                    await _write_row(
                        fresh,
                        action=action,
                        user_id=user_id,
                        target_type=target_type,
                        target_id=target_id,
                        metadata=metadata,
                        ip=ip,
                    )
                    await fresh.commit()
        except Exception as exc:  # noqa: BLE001
            # Never let an audit hiccup propagate.
            log.warning(
                "audit.log(action=%r) failed (non-fatal): %r", action, exc
            )

    def log_background(
        self,
        *,
        action: str,
        user_id: int | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> asyncio.Task | None:
        """Fire-and-forget variant.

        Schedules the write via ``asyncio.create_task`` but **keeps a
        strong reference** to the task in :data:`_BACKGROUND_TASKS` so
        the event loop can't silently drop it. A done-callback logs any
        exception and discards the handle.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. bare sync context in tests); fall
            # back to a best-effort no-op so callers don't need to
            # branch on execution context.
            return None

        coro = self.log(
            action=action,
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
            ip=ip,
        )
        task = loop.create_task(coro)
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_log_task_exception)
        return task


async def _write_row(
    db: AsyncSession,
    *,
    action: str,
    user_id: int | None,
    target_type: str | None,
    target_id: str | None,
    metadata: dict[str, Any] | None,
    ip: str | None,
) -> None:
    """Low-level INSERT helper used by both the sync and bg paths."""
    row = AuditLog(
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=(str(target_id) if target_id is not None else None),
        metadata_json=(
            json.dumps(metadata, default=str, sort_keys=True)
            if metadata
            else None
        ),
        timestamp=_utcnow_iso(),
        ip_address=ip,
    )
    db.add(row)


# Process-wide singleton for convenience.
_audit_service = AuditService()


def get_audit_service() -> AuditService:
    """Return the process-wide :class:`AuditService` instance."""
    return _audit_service
