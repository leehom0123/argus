"""Local username / email + password provider.

Lockout logic (5 failed attempts → 10 min lock) lives here so it's
provider-scoped: future OAuth providers won't touch the same counter.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.password import verify_password
from backend.auth.providers.base import AuthProvider
from backend.config import get_settings
from backend.models import User

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # Accept both "...Z" and "...+00:00" forms.
    cleaned = value.rstrip("Z")
    if cleaned.endswith("+00:00"):
        cleaned = cleaned[:-6]
    try:
        return datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("could not parse locked_until %r", value)
        return None


class AccountLockedError(Exception):
    """Raised when the user exists but is temporarily locked out."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            f"account locked — retry in {retry_after_seconds}s"
        )
        self.retry_after = retry_after_seconds


class LocalAuthProvider(AuthProvider):
    """Classic username / email + argon2 password."""

    name = "local"

    async def authenticate(
        self,
        credentials: dict[str, Any],
        db: AsyncSession,
    ) -> User | None:
        """Resolve by username OR email, check password, manage lockout.

        Returns None on bad password. Raises :class:`AccountLockedError`
        when the account is currently locked — callers turn that into a
        specific 403/429 response. Both paths commit their side-effects
        (counter bump, lock set, last_login set) via the caller's session.
        """
        identifier = str(credentials.get("username_or_email") or "").strip()
        password = credentials.get("password") or ""
        if not identifier or not password:
            return None

        user = await self._lookup(db, identifier)
        if user is None:
            return None

        # Lockout gate
        locked = _parse_iso(user.locked_until)
        now = _utcnow()
        if locked and locked > now:
            retry = int((locked - now).total_seconds())
            raise AccountLockedError(retry_after_seconds=retry)

        if not user.is_active:
            # Banned / deactivated. Same "None" as bad password to avoid
            # leaking active-ness to unauthenticated callers.
            log.info("login attempt on deactivated user %d", user.id)
            return None

        if not verify_password(password, user.password_hash):
            await self._record_failure(db, user)
            return None

        await self._record_success(db, user)
        return user

    # --- helpers ---------------------------------------------------------

    async def _lookup(self, db: AsyncSession, identifier: str) -> User | None:
        ident_lower = identifier.lower()
        stmt = select(User).where(
            (User.username == identifier) | (User.email == ident_lower)
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def _record_failure(self, db: AsyncSession, user: User) -> None:
        settings = get_settings()
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= settings.login_max_failures:
            lock_until = _utcnow() + timedelta(
                minutes=settings.login_lock_minutes
            )
            user.locked_until = _iso(lock_until)
            # Reset the counter when the lock fires so one more failure after
            # the lock period doesn't trip a perma-lock.
            user.failed_login_count = 0
            log.warning(
                "locked user %d until %s (too many failures)",
                user.id,
                user.locked_until,
            )
        await db.commit()

    async def _record_success(self, db: AsyncSession, user: User) -> None:
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login = _iso(_utcnow())
        await db.commit()
