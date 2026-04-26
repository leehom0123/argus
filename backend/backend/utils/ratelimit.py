"""Per-key token-bucket rate limiter.

Used to bound POST /api/events* throughput per API token. Requirements
§4.3 / §6.4 specify **600 requests per minute ≈ 10 req/s** with a modest
burst allowance. We model that as a leaky bucket with capacity 60 and a
refill rate of 10 tokens/sec — a caller that's been idle can fire 60
events instantly, then has to slow down to the sustained rate.

State is in-process. The default 4-worker uvicorn deployment ends up
with a per-worker bucket, so the *aggregate* allowance is roughly 4× the
configured cap; tighten the per-process numbers if you care about the
absolute rate. A shared Redis-backed implementation would fix the drift
but is out of scope for the single-host MVP.

The public surface is deliberately tiny: :meth:`try_consume` is the one
method callers need. It returns ``(allowed, retry_after_seconds)`` so
FastAPI handlers can fold the result directly into a 429 + Retry-After
response.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class _Bucket:
    """Internal state for a single key."""

    tokens: float
    last_refill: float


class TokenBucket:
    """Leaky / token bucket, per-key, async-safe.

    Parameters
    ----------
    capacity:
        Maximum tokens a bucket can hold. Controls the burst size.
    refill_per_sec:
        Tokens added per second while the bucket isn't full. Sets the
        sustained rate. ``capacity / refill_per_sec`` is the refill-to-
        full time.

    The defaults (60 capacity, 10/sec) match requirements §4.3.
    """

    def __init__(
        self,
        capacity: int = 60,
        refill_per_sec: float = 10.0,
    ) -> None:
        if capacity <= 0 or refill_per_sec <= 0:
            raise ValueError("capacity and refill rate must be positive")
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def try_consume(
        self, key: str, cost: float = 1.0
    ) -> tuple[bool, float]:
        """Attempt to remove ``cost`` tokens from the bucket for ``key``.

        Returns ``(allowed, retry_after_seconds)``:
          * ``(True, 0.0)`` — request allowed, bucket debited
          * ``(False, t)`` — request denied, ``t`` seconds until one
            token is available again. Round up in the caller if needed.

        Refill is computed lazily on each call (no background task): we
        only need to know the effective bucket level at the moment of
        the request.
        """
        if cost <= 0:
            raise ValueError("cost must be positive")
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, last_refill=now)
                self._buckets[key] = bucket
            else:
                # Leaky top-up: however long we've been idle, refill by
                # ``elapsed * rate`` up to capacity.
                elapsed = now - bucket.last_refill
                if elapsed > 0:
                    bucket.tokens = min(
                        self.capacity,
                        bucket.tokens + elapsed * self.refill_per_sec,
                    )
                    bucket.last_refill = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0
            # Not enough — compute how long until the missing tokens
            # will have refilled.
            deficit = cost - bucket.tokens
            retry_after = deficit / self.refill_per_sec
            return False, retry_after

    def reset(self) -> None:
        """Clear all bucket state. Test-only helper."""
        self._buckets.clear()

    # Introspection for debugging / tests -------------------------------

    def snapshot(self, key: str) -> tuple[float, float] | None:
        """Return ``(tokens, last_refill)`` for ``key``, or None."""
        b = self._buckets.get(key)
        if b is None:
            return None
        return b.tokens, b.last_refill


# ---------------------------------------------------------------------------
# Module-level singleton used by the ingest middleware. Tests may swap it
# via :func:`reset_default_bucket_for_tests` to avoid cross-test leakage.
# ---------------------------------------------------------------------------

_DEFAULT_BUCKET = TokenBucket(capacity=60, refill_per_sec=10.0)


def get_default_bucket() -> TokenBucket:
    """Return the process-wide ingest rate limiter."""
    return _DEFAULT_BUCKET


def reset_default_bucket_for_tests() -> None:
    """Reset the shared bucket so tests see a clean slate."""
    _DEFAULT_BUCKET.reset()


# ---------------------------------------------------------------------------
# Public / anonymous limiter — keyed by client IP rather than token_hash.
# Capacity + refill are deliberately stricter than the internal reporter
# bucket: anonymous scrapers shouldn't be able to amplify a shared slug
# into a service-level DDoS. Phase-3 post-review M3.
# ---------------------------------------------------------------------------
_PUBLIC_BUCKET = TokenBucket(capacity=30, refill_per_sec=5.0)


def get_public_bucket() -> TokenBucket:
    """Return the process-wide per-IP limiter for anonymous reads."""
    return _PUBLIC_BUCKET


def reset_public_bucket_for_tests() -> None:
    """Reset the anon bucket; called from conftest between tests."""
    _PUBLIC_BUCKET.reset()


# ---------------------------------------------------------------------------
# Password-change limiter — keyed by user_id (``rate-change-password:{user_id}``).
# Capacity 5, refill rate 5/3600 ≈ 0.00139 tokens/sec → burst of 5 per hour.
# Deliberately stricter than the public / ingest buckets because each
# attempt touches argon2 (~200 ms) and triggers an email.
# ---------------------------------------------------------------------------
_CHANGE_PASSWORD_BUCKET = TokenBucket(capacity=5, refill_per_sec=5.0 / 3600.0)


def get_change_password_bucket() -> TokenBucket:
    """Return the process-wide per-user change-password limiter."""
    return _CHANGE_PASSWORD_BUCKET


def reset_change_password_bucket_for_tests() -> None:
    """Reset the change-password bucket between tests."""
    _CHANGE_PASSWORD_BUCKET.reset()


# ---------------------------------------------------------------------------
# Email-change limiter — keyed by user_id (``rate-change-email:{user_id}``).
# Capacity 3, refill rate 3/3600 ≈ 0.000833 tokens/sec → burst of 3 per hour.
# Tighter than change-password because each email-change request mails an
# external (possibly unverified) address; abuse here pollutes inboxes the
# attacker chose, which is a worse failure mode than retrying a password.
# ---------------------------------------------------------------------------
_CHANGE_EMAIL_BUCKET = TokenBucket(capacity=3, refill_per_sec=3.0 / 3600.0)


def get_change_email_bucket() -> TokenBucket:
    """Return the process-wide per-user change-email limiter."""
    return _CHANGE_EMAIL_BUCKET


def reset_change_email_bucket_for_tests() -> None:
    """Reset the change-email bucket between tests."""
    _CHANGE_EMAIL_BUCKET.reset()


# ---------------------------------------------------------------------------
# SMTP-test limiter — keyed by admin user_id (``rate-smtp-test:{admin_id}``).
# Capacity 10, refill rate 10/3600 ≈ 0.00278 tokens/sec → 10 tests per hour.
# Each test opens an outbound TCP connection to a (caller-supplied) host
# and may trigger an SMTP login; abuse turns the admin endpoint into a
# port-scan / credential-stuffing helper, so we cap admins explicitly.
# ---------------------------------------------------------------------------
_SMTP_TEST_BUCKET = TokenBucket(capacity=10, refill_per_sec=10.0 / 3600.0)


def get_smtp_test_bucket() -> TokenBucket:
    """Return the process-wide per-admin SMTP-test limiter."""
    return _SMTP_TEST_BUCKET


def reset_smtp_test_bucket_for_tests() -> None:
    """Reset the SMTP-test bucket between tests."""
    _SMTP_TEST_BUCKET.reset()


# ---------------------------------------------------------------------------
# Resend-verification limiter — keyed by user_id
# (``rate-resend-verification:{user_id}``). Capacity 1, refill rate
# 1/60 ≈ 0.01667 tokens/sec → at most 1 resend per minute (#108).
# Each resend issues a fresh email; we cap aggressively because a
# tight client retry loop here would burn through the operator's SMTP
# quota (and spam the user's inbox).
# ---------------------------------------------------------------------------
_RESEND_VERIFICATION_BUCKET = TokenBucket(capacity=1, refill_per_sec=1.0 / 60.0)


def get_resend_verification_bucket() -> TokenBucket:
    """Return the process-wide per-user resend-verification limiter."""
    return _RESEND_VERIFICATION_BUCKET


def reset_resend_verification_bucket_for_tests() -> None:
    """Reset the resend-verification bucket between tests."""
    _RESEND_VERIFICATION_BUCKET.reset()
