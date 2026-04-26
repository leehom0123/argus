"""Admin-toggleable global feature flags.

Values are JSON-encoded in the ``feature_flag.value_json`` column so we
can hold bools, ints, strings, and small dicts uniformly. Defaults live
here (not in the DB) so a fresh install works without explicit seeding.

Typical usage from route handlers:

    from backend.services.feature_flags import get_flag

    if not await get_flag(db, "registration_open", default=True):
        raise HTTPException(403, "Registration disabled")

Writes go through :func:`set_flag`, which records the updater's id
and bumps ``updated_at``. The admin router uses this to implement
``PUT /api/admin/feature-flags/{key}``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import FeatureFlag

log = logging.getLogger(__name__)


# Default values for the three flags the MVP cares about
# (requirements §7.6 + §16.5). Unknown keys return whatever ``default``
# the caller passes in, so future flags don't need a code change here.
DEFAULT_FLAGS: dict[str, Any] = {
    "registration_open": True,
    "stalled_threshold_sec": 300,
    "email_verification_required": False,
}


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


async def get_flag(
    db: AsyncSession,
    key: str,
    default: Any | None = None,
) -> Any:
    """Return the current value for ``key``.

    Falls back to :data:`DEFAULT_FLAGS` before finally returning
    ``default`` (which itself defaults to ``None``). Corrupt JSON in the
    DB is logged and treated as "flag missing" to avoid 500s.
    """
    row = await db.get(FeatureFlag, key)
    if row is None:
        if key in DEFAULT_FLAGS:
            return DEFAULT_FLAGS[key]
        return default
    try:
        return json.loads(row.value_json)
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning(
            "feature_flag %r has invalid JSON %r: %s",
            key,
            row.value_json,
            exc,
        )
        if key in DEFAULT_FLAGS:
            return DEFAULT_FLAGS[key]
        return default


async def set_flag(
    db: AsyncSession,
    key: str,
    value: Any,
    updated_by: int | None = None,
) -> FeatureFlag:
    """Upsert a flag value.

    Returns the persisted row. Caller is responsible for
    ``await db.commit()`` so the write can participate in a larger
    transaction if needed.
    """
    row = await db.get(FeatureFlag, key)
    payload = json.dumps(value, default=str, sort_keys=True)
    now = _utcnow_iso()
    if row is None:
        row = FeatureFlag(
            key=key,
            value_json=payload,
            updated_at=now,
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.value_json = payload
        row.updated_at = now
        row.updated_by = updated_by
    return row


async def list_flags(db: AsyncSession) -> dict[str, Any]:
    """Return the full flag set, merging DB overrides onto defaults."""
    from sqlalchemy import select

    result = await db.execute(select(FeatureFlag))
    merged: dict[str, Any] = dict(DEFAULT_FLAGS)
    for row in result.scalars():
        try:
            merged[row.key] = json.loads(row.value_json)
        except (json.JSONDecodeError, TypeError):
            log.warning(
                "skipping feature_flag %r: invalid JSON %r",
                row.key,
                row.value_json,
            )
    return merged
