"""Tests for GET /api/hosts/{host}/timeseries.

Covers:
  - 3-batch stacking: 3 concurrent batches each contributing different
    proc_gpu_mem_mb values across 5 buckets
  - Fallback when proc_* columns are absent (by_batch stays empty)
  - 404 when the host has never posted a snapshot
  - metric=gpu_util_pct with only host-level data (by_batch={})
  - Empty bucket window when host exists but no data in range

Note: ``ResourceSnapshot`` does not yet have ``batch_id`` /
``proc_gpu_mem_mb`` / ``proc_ram_mb`` / ``proc_cpu_pct`` columns —
those land in PR-A (migration 008). This test suite seeds snapshots
via ``POST /api/events`` (resource_snapshot event) which stores data
in the ORM columns. The ``getattr`` fallbacks in the service mean
every ``by_batch`` dict will be empty until PR-A columns exist.

Once PR-A lands and the ORM/migration are applied, the 3-batch test
can be upgraded to verify non-empty ``by_batch`` values by seeding
snapshots with direct DB insertion.
"""
from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snap_event(
    host: str,
    ts: str,
    gpu_mem_mb: float = 2000.0,
    gpu_util_pct: float = 50.0,
    gpu_mem_total_mb: float = 24000.0,
    ram_mb: float = 8000.0,
    ram_total_mb: float = 64000.0,
    batch_id: str | None = None,
) -> dict:
    """Build a resource_snapshot event dict."""
    if batch_id is None:
        batch_id = f"snap-{uuid.uuid4().hex[:8]}"
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "resource_snapshot",
        "timestamp": ts,
        "batch_id": batch_id,
        "source": {"project": "test", "host": host},
        "data": {
            "gpu_util_pct": gpu_util_pct,
            "gpu_mem_mb": gpu_mem_mb,
            "gpu_mem_total_mb": gpu_mem_total_mb,
            "cpu_util_pct": 30.0,
            "ram_mb": ram_mb,
            "ram_total_mb": ram_total_mb,
            "disk_free_mb": 100_000.0,
        },
    }


async def _post_snap(client, **kwargs) -> None:
    ev = _snap_event(**kwargs)
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_timeseries_404_unknown_host(client):
    """404 when the host has never posted any snapshot."""
    r = await client.get("/api/hosts/ghost-host/timeseries")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_host_timeseries_basic_buckets(client):
    """Single host, 5 snapshots → correct bucket structure and total."""
    host = "lab-ts-1"
    # Post 5 snapshots across 5 different minutes (bucket_seconds=60).
    for i in range(5):
        await _post_snap(
            client,
            host=host,
            ts=f"2026-04-24T10:0{i}:00Z",
            gpu_mem_mb=float(1000 + i * 500),
            batch_id="bench-aaa",
        )

    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={
            "metric": "gpu_mem_mb",
            "since": "2026-04-24T09:55:00Z",
            "bucket_seconds": 60,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["host"] == host
    assert body["metric"] == "gpu_mem_mb"
    assert isinstance(body["buckets"], list)
    assert len(body["buckets"]) == 5

    # Each bucket must have the required shape.
    for bucket in body["buckets"]:
        assert "ts" in bucket
        assert "total" in bucket
        assert "by_batch" in bucket
        assert isinstance(bucket["by_batch"], dict)

    # host_total_capacity comes from gpu_mem_total_mb.
    assert body["host_total_capacity"] == 24000.0


@pytest.mark.asyncio
async def test_host_timeseries_three_batches(client):
    """3 batches posting snapshots simultaneously → buckets have correct totals.

    ``by_batch`` will be empty until PR-A (migration 008) columns land;
    we only assert ``total`` values here to keep the test forward-
    compatible.
    """
    host = "lab-ts-2"
    # All three batches post at minute :00.
    for i, (bid, mem) in enumerate(
        [("bench-x", 3000.0), ("bench-y", 5000.0), ("bench-z", 2000.0)]
    ):
        await _post_snap(
            client,
            host=host,
            ts="2026-04-24T11:00:00Z",
            gpu_mem_mb=mem,
            batch_id=bid,
        )
    # Second minute has only two batches.
    for bid, mem in [("bench-x", 3500.0), ("bench-y", 4500.0)]:
        await _post_snap(
            client,
            host=host,
            ts="2026-04-24T11:01:00Z",
            gpu_mem_mb=mem,
            batch_id=bid,
        )

    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={
            "metric": "gpu_mem_mb",
            "since": "2026-04-24T10:58:00Z",
            "bucket_seconds": 60,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert len(body["buckets"]) == 2

    # Bucket 0: three snapshots with gpu_mem_mb = 3000, 5000, 2000.
    # total = average = (3000+5000+2000)/3 ≈ 3333.33
    bucket0 = body["buckets"][0]
    assert bucket0["total"] is not None
    assert abs(bucket0["total"] - 10000 / 3) < 1.0

    # Bucket 1: two snapshots → average = (3500+4500)/2 = 4000
    bucket1 = body["buckets"][1]
    assert bucket1["total"] is not None
    assert abs(bucket1["total"] - 4000.0) < 1.0


@pytest.mark.asyncio
async def test_host_timeseries_empty_window(client):
    """Host has data but none in the requested window → 200 with empty buckets."""
    host = "lab-ts-3"
    # Post a snapshot in the past.
    await _post_snap(
        client,
        host=host,
        ts="2026-01-01T00:00:00Z",
        batch_id="old-batch",
    )

    # Request a window that doesn't include the old snapshot.
    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={
            "metric": "gpu_mem_mb",
            "since": "2026-04-24T00:00:00Z",
            "bucket_seconds": 60,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host"] == host
    assert body["buckets"] == []
    assert body["host_total_capacity"] is None


@pytest.mark.asyncio
async def test_host_timeseries_gpu_util_pct_capacity_100(client):
    """gpu_util_pct metric → host_total_capacity = 100.0."""
    host = "lab-ts-4"
    await _post_snap(
        client,
        host=host,
        ts="2026-04-24T12:00:00Z",
        gpu_util_pct=80.0,
        batch_id="bench-q",
    )

    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={
            "metric": "gpu_util_pct",
            "since": "2026-04-24T11:58:00Z",
            "bucket_seconds": 60,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host_total_capacity"] == 100.0
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["total"] == 80.0
    # by_batch is empty because proc_cpu_pct column doesn't exist yet (pre-PR-A).
    assert body["buckets"][0]["by_batch"] == {}


@pytest.mark.asyncio
async def test_host_timeseries_relative_since(client):
    """Relative 'now-2h' syntax is accepted without error."""
    host = "lab-ts-5"
    await _post_snap(
        client,
        host=host,
        ts="2026-04-24T12:00:00Z",
        batch_id="bench-r",
    )

    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={"metric": "gpu_mem_mb", "since": "now-2h", "bucket_seconds": 60},
    )
    # The snapshot timestamp is old (fixed date), so it may not fall in the
    # relative 2h window — we just assert the endpoint returns 200, not 5xx.
    assert r.status_code == 200, r.text
    body = r.json()
    assert "buckets" in body


@pytest.mark.asyncio
async def test_host_timeseries_invalid_metric_defaults_to_gpu_mem(client):
    """Unknown metric value is silently coerced to gpu_mem_mb."""
    host = "lab-ts-6"
    await _post_snap(
        client,
        host=host,
        ts="2026-04-24T13:00:00Z",
        batch_id="bench-s",
    )

    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={
            "metric": "nonsense_metric",
            "since": "2026-04-24T12:58:00Z",
            "bucket_seconds": 60,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Falls back to gpu_mem_mb.
    assert body["metric"] == "gpu_mem_mb"
