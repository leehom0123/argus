"""Personal API token primitives.

An API token is a 28+ char opaque string made of a fixed prefix
(``em_live_`` / ``em_view_``) plus 27-ish URL-safe random chars. We store
only the SHA-256 hash; plaintext is shown exactly once at creation.

Lookup helper :func:`lookup_token` returns the matching ``ApiToken`` row
(with the owning ``User`` eagerly loaded) or ``None`` — callers decide
whether that means 401 (API endpoint) or just "unknown token" (internal).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import ApiToken

log = logging.getLogger(__name__)


Scope = Literal["reporter", "viewer"]

# Prefix + scope pairing is fixed: we never want a caller to invent a
# mixed-up combination (``em_live_`` + ``viewer`` etc.). The scope decides
# what the token is *allowed* to do; the prefix is cosmetic but documented
# in requirements §4.1 so we keep them aligned.
SCOPE_TO_PREFIX: dict[Scope, str] = {
    "reporter": "em_live_",
    "viewer": "em_view_",
}
PREFIX_TO_SCOPE: dict[str, Scope] = {v: k for k, v in SCOPE_TO_PREFIX.items()}

# Number of random bytes. 20 → ~27 chars of base64url which has ~160 bits
# of entropy — overkill but keeps brute force off the table.
_RANDOM_BYTES = 20

# How many leading plaintext chars we surface back to the UI for display.
# Longer hints leak too much secret; shorter hints are ambiguous. 8 is
# what GitHub/GitLab both use for their "visible id" of a PAT.
_DISPLAY_HINT_CHARS = 8


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string ending in Z."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def hash_token(token: str) -> str:
    """Return ``SHA-256`` hex digest of the plaintext token.

    Kept as a thin named helper (rather than inlining ``hashlib.sha256``)
    so the contract is explicit and every caller produces identical
    results. The digest is deterministic, so lookup by hash is O(log n)
    via the ``token_hash`` index.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_api_token(scope: Scope) -> tuple[str, str, str, str]:
    """Mint a fresh API token.

    Returns ``(plaintext, token_hash, prefix, display_hint)``.

    The plaintext is the only form ever exposed to the user; backend
    stores the other three values. ``display_hint`` is the first 8 chars
    of plaintext — useful for UI disambiguation and intentionally too
    short to be usable as a credential on its own.
    """
    if scope not in SCOPE_TO_PREFIX:
        raise ValueError(f"unknown token scope {scope!r}")
    prefix = SCOPE_TO_PREFIX[scope]
    body = secrets.token_urlsafe(_RANDOM_BYTES)
    plaintext = f"{prefix}{body}"
    return plaintext, hash_token(plaintext), prefix, plaintext[:_DISPLAY_HINT_CHARS]


def token_is_expired(expires_at: str | None) -> bool:
    """Return True iff ``expires_at`` is non-null and in the past.

    ISO 8601 parsing handles both ``...Z`` and ``...+00:00`` forms so
    stored rows from different code paths compare correctly.
    """
    if not expires_at:
        return False
    cleaned = expires_at.rstrip("Z")
    if cleaned.endswith("+00:00"):
        cleaned = cleaned[:-6]
    try:
        exp_dt = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    except ValueError:
        # Corrupt data — treat as expired rather than silently accepting.
        log.warning("token expires_at %r could not be parsed", expires_at)
        return True
    return exp_dt <= datetime.now(timezone.utc)


async def lookup_token(
    db: AsyncSession, token: str
) -> ApiToken | None:
    """Resolve a plaintext token to its :class:`ApiToken` row.

    Returns ``None`` when the token is unknown, revoked, or expired.
    The owning ``User`` is eagerly loaded via ``selectinload`` so the
    caller doesn't trigger an implicit second round-trip inside an
    ``async def`` context (which can fail with ``MissingGreenlet``).
    """
    if not token:
        return None

    # Gate on prefix to avoid hashing arbitrary junk. This also keeps the
    # expensive constant-time SHA path off the hot JWT code path.
    if not (token.startswith("em_live_") or token.startswith("em_view_")):
        return None

    digest = hash_token(token)
    stmt = select(ApiToken).where(
        ApiToken.token_hash == digest, ApiToken.revoked.is_(False)
    )
    row: ApiToken | None = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    if token_is_expired(row.expires_at):
        return None
    return row


async def touch_last_used(
    db: AsyncSession, token_id: int
) -> None:
    """Best-effort update of ``last_used``.

    Intentionally swallow errors: bumping ``last_used`` is observability,
    not correctness. A DB hiccup here should not turn a valid token into
    a 500 for the caller.
    """
    try:
        row = await db.get(ApiToken, token_id)
        if row is None:
            return
        row.last_used = _utcnow_iso()
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("touch_last_used(%d) failed: %s", token_id, exc)
