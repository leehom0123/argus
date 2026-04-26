"""Tests for the per-(user, event_type) email rate limit (Team Email / BE-2)."""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_first_call_allowed_second_denied():
    from backend.services import email_rate_limit as rl

    rl.reset_email_bucket_for_tests()
    assert await rl.is_allowed(1, "job_finished") is True
    assert await rl.is_allowed(1, "job_finished") is False


@pytest.mark.asyncio
async def test_key_isolates_by_user_and_event_type():
    from backend.services import email_rate_limit as rl

    rl.reset_email_bucket_for_tests()
    assert await rl.is_allowed(1, "job_finished") is True
    assert await rl.is_allowed(2, "job_finished") is True
    assert await rl.is_allowed(1, "batch_complete") is True
    assert await rl.is_allowed(1, "job_finished") is False
    assert await rl.is_allowed(2, "job_finished") is False
    assert await rl.is_allowed(1, "batch_complete") is False


@pytest.mark.asyncio
async def test_refill_after_window(monkeypatch):
    from backend.services import email_rate_limit as rl
    from backend.utils.ratelimit import TokenBucket

    fast = TokenBucket(capacity=1, refill_per_sec=50.0)
    monkeypatch.setattr(rl, "_EMAIL_BUCKET", fast)
    assert await rl.is_allowed(7, "x") is True
    assert await rl.is_allowed(7, "x") is False
    await asyncio.sleep(0.05)
    assert await rl.is_allowed(7, "x") is True


def test_key_shape():
    from backend.services.email_rate_limit import _key

    assert _key(42, "job_finished") == "email:42:job_finished"
