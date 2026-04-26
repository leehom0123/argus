"""Auth-boundary + edge-case tests for GET /api/hosts/{host}/timeseries.

Fills the gaps left by test_hosts_timeseries.py, which covers happy-path
bucket shapes but never touches:
- 401 when no JWT is supplied
- bucket_seconds boundary clamps (ge=10, le=3600)
- since=now-{minutes} and since=now-{seconds} relative formats
- ram_mb + cpu_util_pct metrics (only gpu_mem_mb / gpu_util_pct were exercised)
- host_total_capacity set correctly for each metric kind

Commit scope: 5c017cb + 45903d4 + 7d43052 (timeseries endpoint + proc snapshot)
"""
from __future__ import annotations

import uuid

import pytest

from backend.tests._dashboard_helpers import post_event


# ---------------------------------------------------------------------------
# Helpers (duplicated to keep this file self-contained)
# ---------------------------------------------------------------------------


def _snap(
    host: str,
    ts: str,
    batch_id: str | None = None,
    gpu_mem_mb: float = 3000.0,
    gpu_util_pct: float = 55.0,
    gpu_mem_total_mb: float = 24000.0,
    ram_mb: float = 16000.0,
    ram_total_mb: float = 64000.0,
    cpu_util_pct: float = 40.0,
) -> dict:
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
            "cpu_util_pct": cpu_util_pct,
            "ram_mb": ram_mb,
            "ram_total_mb": ram_total_mb,
        },
    }


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_timeseries_requires_auth(unauthed_client):
    """No JWT → 401 (endpoint uses Depends(get_current_user))."""
    r = await unauthed_client.get("/api/hosts/any-host/timeseries")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_host_timeseries_bearer_accepted(client):
    """Valid JWT (reporter token, which is the conftest default) → not 401."""
    host = "auth-check-host"
    await post_event(client, _snap(host, "2026-04-24T10:00:00Z"))
    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={"since": "2026-04-24T09:50:00Z"},
    )
    # 200 or 404 (snapshot may fall outside window) — just not 401/403
    assert r.status_code in (200, 404), r.text


# ---------------------------------------------------------------------------
# bucket_seconds clamping (FastAPI Query ge=10, le=3600)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_timeseries_bucket_seconds_too_small_rejected(client):
    """bucket_seconds < 10 → 422 Unprocessable Entity."""
    r = await client.get(
        "/api/hosts/some-host/timeseries",
        params={"bucket_seconds": 5},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_host_timeseries_bucket_seconds_too_large_rejected(client):
    """bucket_seconds > 3600 → 422 Unprocessable Entity."""
    r = await client.get(
        "/api/hosts/some-host/timeseries",
        params={"bucket_seconds": 9999},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_host_timeseries_bucket_seconds_boundaries_accepted(client):
    """bucket_seconds = 10 and 3600 are valid (ge=10, le=3600)."""
    host = "bsec-clamp-host"
    await post_event(client, _snap(host, "2026-04-24T10:00:00Z"))
    for bsec in (10, 3600):
        r = await client.get(
            f"/api/hosts/{host}/timeseries",
            params={"bucket_seconds": bsec},
        )
        assert r.status_code == 200, f"bucket_seconds={bsec}: {r.text}"


# ---------------------------------------------------------------------------
# Relative `since` syntax — minutes and seconds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_timeseries_relative_since_minutes(client):
    """since='now-30m' is accepted and returns 200."""
    host = "relm-host"
    await post_event(client, _snap(host, "2026-04-24T10:00:00Z"))
    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={"since": "now-30m"},
    )
    assert r.status_code == 200, r.text
    assert "buckets" in r.json()


@pytest.mark.asyncio
async def test_host_timeseries_relative_since_seconds(client):
    """since='now-120s' is accepted and returns 200."""
    host = "rels-host"
    await post_event(client, _snap(host, "2026-04-24T10:00:00Z"))
    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={"since": "now-120s"},
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# ram_mb metric — host_total_capacity = ram_total_mb
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_timeseries_ram_metric_capacity(client):
    """metric=ram_mb → host_total_capacity comes from ram_total_mb."""
    host = "ram-cap-host"
    await post_event(
        client,
        _snap(host, "2026-04-24T11:00:00Z", ram_mb=16000.0, ram_total_mb=65536.0),
    )
    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={
            "metric": "ram_mb",
            "since": "2026-04-24T10:50:00Z",
            "bucket_seconds": 60,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metric"] == "ram_mb"
    # host_total_capacity must equal ram_total_mb from the snapshot
    assert body["host_total_capacity"] == pytest.approx(65536.0)


# ---------------------------------------------------------------------------
# cpu_util_pct metric — host_total_capacity = 100
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_timeseries_cpu_metric_capacity_100(client):
    """metric=cpu_util_pct → host_total_capacity is 100 (percentage max)."""
    host = "cpu-cap-host"
    await post_event(
        client,
        _snap(host, "2026-04-24T11:30:00Z", cpu_util_pct=72.0),
    )
    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={
            "metric": "cpu_util_pct",
            "since": "2026-04-24T11:20:00Z",
            "bucket_seconds": 60,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metric"] == "cpu_util_pct"
    assert body["host_total_capacity"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Response schema is valid HostTimeseriesOut
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_timeseries_response_schema_fields(client):
    """Response always contains host, metric, buckets, host_total_capacity."""
    host = "schema-host"
    await post_event(client, _snap(host, "2026-04-24T11:00:00Z"))
    r = await client.get(
        f"/api/hosts/{host}/timeseries",
        params={"since": "2026-04-24T10:50:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "host" in body
    assert "metric" in body
    assert "buckets" in body
    assert "host_total_capacity" in body
    assert body["host"] == host
    for bucket in body["buckets"]:
        assert "ts" in bucket
        assert "total" in bucket
        assert "by_batch" in bucket
        assert isinstance(bucket["by_batch"], dict)
