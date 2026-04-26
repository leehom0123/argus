"""Per-(user_id, event_type) sliding rate limit for outbound email.

5-minute sliding window on backend.utils.ratelimit.TokenBucket.
Capacity 1, refill 1 token per 300 s -> one email per (user,
event_type) every five minutes. Denials log at debug only to avoid
drowning production logs when a user triggers 40 of the same event
in a minute.
"""
from __future__ import annotations

import logging

from backend.utils.ratelimit import TokenBucket

log = logging.getLogger(__name__)

_EMAIL_BUCKET = TokenBucket(capacity=1, refill_per_sec=1.0 / 300.0)


def _key(user_id, event_type: str) -> str:
    return f"email:{user_id}:{event_type}"


async def is_allowed(user_id, event_type: str) -> bool:
    """Return True when the (user, event_type) pair is within quota."""
    allowed, retry_after = await _EMAIL_BUCKET.try_consume(
        _key(user_id, event_type)
    )
    if not allowed:
        log.debug(
            "email.rate_limited user_id=%s event_type=%s retry_after=%.1fs",
            user_id, event_type, retry_after,
        )
    return allowed


def reset_email_bucket_for_tests() -> None:
    """Drop all window state. Called from conftest between tests."""
    _EMAIL_BUCKET.reset()


def get_email_bucket() -> TokenBucket:
    """Underlying bucket for tests that want .snapshot()."""
    return _EMAIL_BUCKET
