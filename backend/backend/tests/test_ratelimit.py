"""Rate limiting — TokenBucket unit + ingest integration."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.utils.ratelimit import TokenBucket


def _ev(i: int) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_epoch",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": "b-rl",
        "job_id": f"j-{i}",
        "source": {"project": "p"},
        "data": {"epoch": 1, "train_loss": 0.5},
    }


# -------------------------- Unit tests on TokenBucket ---------------------


@pytest.mark.asyncio
async def test_token_bucket_refills_over_time():
    bucket = TokenBucket(capacity=2, refill_per_sec=10.0)
    # Drain the bucket.
    allowed, _ = await bucket.try_consume("k")
    assert allowed
    allowed, _ = await bucket.try_consume("k")
    assert allowed
    allowed, retry_after = await bucket.try_consume("k")
    assert not allowed
    assert retry_after > 0
    # Wait long enough for one token to refill (10/s → 0.1s / token).
    await asyncio.sleep(0.15)
    allowed, _ = await bucket.try_consume("k")
    assert allowed


@pytest.mark.asyncio
async def test_token_bucket_retry_after_is_reasonable():
    bucket = TokenBucket(capacity=1, refill_per_sec=5.0)
    await bucket.try_consume("k")  # drain
    allowed, retry_after = await bucket.try_consume("k")
    assert not allowed
    # At 5/s the next token arrives in 0.2s.
    assert 0.0 < retry_after <= 0.21


# -------------------------- Ingest integration -----------------------------


@pytest.mark.asyncio
async def test_ingest_enforces_rate_limit(client, monkeypatch):
    """Burst beyond capacity gets 429 with a Retry-After header.

    We inject a fresh small-capacity bucket so the test doesn't have to
    fire thousands of requests to exhaust the production 60/10 bucket.
    """
    from backend.utils import ratelimit as rl

    tiny = TokenBucket(capacity=3, refill_per_sec=1.0)
    monkeypatch.setattr(rl, "_DEFAULT_BUCKET", tiny)
    monkeypatch.setattr(rl, "get_default_bucket", lambda: tiny)

    # Three allowed…
    for i in range(3):
        r = await client.post("/api/events", json=_ev(i))
        assert r.status_code == 200, (i, r.text)

    # Fourth should 429.
    r = await client.post("/api/events", json=_ev(99))
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers.keys()}
    # Retry-After is an integer second count per RFC 7231.
    retry_after = int(r.headers.get("retry-after"))
    assert retry_after >= 1


@pytest.mark.asyncio
async def test_ingest_rate_limit_is_per_token(client, monkeypatch):
    """Two separate tokens have independent buckets."""
    from backend.utils import ratelimit as rl

    tiny = TokenBucket(capacity=1, refill_per_sec=0.1)
    monkeypatch.setattr(rl, "_DEFAULT_BUCKET", tiny)
    monkeypatch.setattr(rl, "get_default_bucket", lambda: tiny)

    # The default (tester) token drains its bucket first.
    r1 = await client.post("/api/events", json=_ev(0))
    assert r1.status_code == 200
    # Second request with same token → 429
    r2 = await client.post("/api/events", json=_ev(1))
    assert r2.status_code == 429

    # Mint a second token and use it — fresh bucket, should still pass.
    jwt = getattr(client, "_test_default_jwt")
    cr = await client.post(
        "/api/tokens",
        json={"name": "second", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    other_token = cr.json()["token"]
    r3 = await client.post(
        "/api/events",
        json=_ev(2),
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert r3.status_code == 200
