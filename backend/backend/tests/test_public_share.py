"""Public share endpoints.

Covers: slug generation, anonymous GET, view_count bump, expiry → 410,
owner-only revoke.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _event(batch_id: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "proj", "host": "h1"},
        "data": {"n_total_jobs": 1, "experiment_type": "forecast"},
    }


def _iso_in(delta: timedelta) -> str:
    t = datetime.now(timezone.utc) + delta
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_generate_slug_and_anonymous_access(client):
    """POST creates a slug; GET /api/public/{slug} works without auth."""
    await client.post("/api/events", json=_event("pub-1"))
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.post(
        "/api/batches/pub-1/public-share",
        json={},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201, r.text
    slug = r.json()["slug"]
    assert len(slug) == 20  # secrets.token_urlsafe(15) → 20 chars

    # Anonymous access (strip Authorization header)
    saved = client.headers.pop("Authorization", None)
    try:
        r = await client.get(f"/api/public/{slug}")
        assert r.status_code == 200, r.text
        body = r.json()
        # Owner PII stripped
        assert "owner_id" not in body
        assert "email" not in body
        assert body["owner_label"].startswith("Shared by user")
        assert body["id"] == "pub-1"
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_view_count_increments(client):
    """Each GET bumps view_count and last_viewed."""
    await client.post("/api/events", json=_event("pub-2"))
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.post(
        "/api/batches/pub-2/public-share",
        json={},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    slug = r.json()["slug"]

    saved = client.headers.pop("Authorization", None)
    try:
        for _ in range(3):
            resp = await client.get(f"/api/public/{slug}")
            assert resp.status_code == 200
    finally:
        if saved:
            client.headers["Authorization"] = saved

    # Owner-side list should show view_count=3
    r = await client.get(
        "/api/batches/pub-2/public-shares",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    rows = r.json()
    assert rows[0]["view_count"] == 3
    assert rows[0]["last_viewed"] is not None


@pytest.mark.asyncio
async def test_expired_share_returns_410(client):
    """expires_at in the past → 410 Gone."""
    await client.post("/api/events", json=_event("pub-3"))
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    past = _iso_in(timedelta(hours=-1))
    r = await client.post(
        "/api/batches/pub-3/public-share",
        json={"expires_at": past},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201
    slug = r.json()["slug"]

    saved = client.headers.pop("Authorization", None)
    try:
        r = await client.get(f"/api/public/{slug}")
        assert r.status_code == 410
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_revoke_slug_breaks_access(client):
    """DELETE the slug → anonymous GET returns 404."""
    await client.post("/api/events", json=_event("pub-4"))
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.post(
        "/api/batches/pub-4/public-share",
        json={},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    slug = r.json()["slug"]

    r = await client.delete(
        f"/api/batches/pub-4/public-share/{slug}",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200

    saved = client.headers.pop("Authorization", None)
    try:
        r = await client.get(f"/api/public/{slug}")
        assert r.status_code == 404
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_public_rate_limit(client, monkeypatch):
    """Anonymous GETs on a slug are capped by a per-IP token bucket.

    Phase-3 post-review M3: without this an attacker who scrapes a
    shared slug could pin a worker indefinitely. We shrink the
    process-wide public bucket into a tight 5-token fixture so the test
    can confirm both the 200 burst and the 429 cutoff without spending
    seconds waiting for refills.
    """
    from backend.utils import ratelimit as rl
    from backend.utils.ratelimit import TokenBucket

    # Create a slug as tester then hit it anonymously until we hit 429.
    await client.post("/api/events", json=_event("pub-rl"))
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/batches/pub-rl/public-share",
        json={},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    slug = r.json()["slug"]

    # Swap the anon limiter for a tiny bucket so the test is deterministic.
    tiny = TokenBucket(capacity=5, refill_per_sec=0.01)
    monkeypatch.setattr(rl, "_PUBLIC_BUCKET", tiny)
    monkeypatch.setattr(rl, "get_public_bucket", lambda: tiny)

    saved = client.headers.pop("Authorization", None)
    try:
        # 5 × 200 OK — bucket drains from capacity.
        for _ in range(5):
            resp = await client.get(f"/api/public/{slug}")
            assert resp.status_code == 200, resp.text
        # 6th burst request → 429 + Retry-After header.
        resp = await client.get(f"/api/public/{slug}")
        assert resp.status_code == 429
        assert "retry-after" in {k.lower() for k in resp.headers.keys()}
        retry_after = int(resp.headers.get("retry-after"))
        assert retry_after >= 1
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_jobs_and_epochs_endpoints(client):
    """Anonymous jobs + epochs paths serve the same shape as authenticated ones."""
    # Seed batch + job + epoch events as tester.
    await client.post("/api/events", json=_event("pub-5"))
    await client.post(
        "/api/events",
        json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_start",
            "timestamp": "2026-04-23T09:01:00Z",
            "batch_id": "pub-5",
            "job_id": "j1",
            "source": {"project": "proj"},
            "data": {"model": "transformer", "dataset": "etth1"},
        },
    )
    await client.post(
        "/api/events",
        json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_epoch",
            "timestamp": "2026-04-23T09:01:10Z",
            "batch_id": "pub-5",
            "job_id": "j1",
            "source": {"project": "proj"},
            "data": {"epoch": 1, "train_loss": 0.4, "val_loss": 0.5},
        },
    )

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/batches/pub-5/public-share",
        json={},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    slug = r.json()["slug"]

    saved = client.headers.pop("Authorization", None)
    try:
        jobs = await client.get(f"/api/public/{slug}/jobs")
        assert jobs.status_code == 200
        assert jobs.json()[0]["id"] == "j1"

        single = await client.get(f"/api/public/{slug}/jobs/j1")
        assert single.status_code == 200
        assert single.json()["model"] == "transformer"

        epochs = await client.get(f"/api/public/{slug}/jobs/j1/epochs")
        assert epochs.status_code == 200
        pts = epochs.json()
        assert pts and pts[0]["epoch"] == 1
    finally:
        if saved:
            client.headers["Authorization"] = saved
