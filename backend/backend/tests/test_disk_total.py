"""Disk-total propagation tests for migration 020 + reporter rollout.

The DeepTS reporter (``scripts/common/resource_snapshot.py``) was updated
to emit ``disk_total_mb`` so the frontend can render a real "disk used%"
bar instead of the legacy free-GB pressure heuristic.

These tests cover the round-trip:

1. A reporter ``resource_snapshot`` event carrying ``disk_total_mb`` is
   accepted, persisted, and surfaced through ``GET /api/resources``
   exactly as it was sent.
2. Older reporters (no ``disk_total_mb`` field) still work — the column
   is nullable, so missing values land as ``None`` and don't break the
   ingest path.

The frontend's bar logic (used% from total - free; fallback when total is
null) is exercised in the Vue component tests; here we just verify the
backend wire side.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_disk_total_mb_round_trip(client):
    """A snapshot with ``disk_total_mb`` survives ingest → DB → resources GET."""
    ev = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "resource_snapshot",
        "timestamp": "2026-04-25T09:00:00Z",
        "batch_id": "disk-total-rt-1",
        "source": {"project": "p", "host": "host-a", "user": "u"},
        "data": {
            "gpu_util_pct": 42,
            "gpu_mem_mb": 5000,
            "gpu_mem_total_mb": 24000,
            "ram_mb": 8000,
            "ram_total_mb": 64000,
            "disk_free_mb": 120_000,
            # Total capacity = 1 TB. With free=120 GB, used% = 88.3%.
            "disk_total_mb": 1_024_000,
        },
    }
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200, r.text

    r = await client.get("/api/resources", params={"host": "host-a"})
    assert r.status_code == 200
    snaps = r.json()
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap["disk_free_mb"] == 120_000
    # Round-trip preserves the new column unchanged — this is what the
    # frontend hooks into for the used% formula.
    assert snap["disk_total_mb"] == 1_024_000


@pytest.mark.asyncio
async def test_disk_total_mb_nullable_backward_compat(client):
    """Legacy reporters without ``disk_total_mb`` still ingest fine.

    The old wire format only carried ``disk_free_mb`` — verify the
    backend accepts the snapshot, stores ``None`` for ``disk_total_mb``,
    and the resources endpoint surfaces the absence as null. The
    frontend treats null as "use the legacy free-GB fallback".
    """
    ev = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "resource_snapshot",
        "timestamp": "2026-04-25T09:01:00Z",
        "batch_id": "disk-total-rt-2",
        "source": {"project": "p", "host": "host-b", "user": "u"},
        "data": {
            "gpu_util_pct": 10,
            "disk_free_mb": 50_000,
            # No disk_total_mb — older reporter.
        },
    }
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200, r.text

    r = await client.get("/api/resources", params={"host": "host-b"})
    assert r.status_code == 200
    snaps = r.json()
    assert len(snaps) == 1
    assert snaps[0]["disk_free_mb"] == 50_000
    assert snaps[0]["disk_total_mb"] is None


@pytest.mark.asyncio
async def test_disk_total_mb_appears_in_dashboard_host_card(client):
    """When set on the latest snapshot, the dashboard host card surfaces it.

    The dashboard's host card includes ``disk_total_mb`` so HostCapacityChip
    can render the real used% bar without an extra timeseries fetch.

    Note: ``_host_cards`` only surfaces hosts whose latest snapshot is
    within the last 5 minutes (active-host filter), so the fixture
    timestamp is anchored to wall-clock now() rather than a hardcoded
    string that would silently fall outside the window once the test
    suite is run on a later date.
    """
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "resource_snapshot",
        "timestamp": ts_now,
        "batch_id": "disk-total-rt-3",
        "source": {"project": "p", "host": "host-c", "user": "u"},
        "data": {
            "gpu_util_pct": 30,
            "ram_mb": 2000,
            "ram_total_mb": 32000,
            "disk_free_mb": 200_000,
            "disk_total_mb": 500_000,
        },
    }
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200

    r = await client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    hosts = body.get("hosts", [])
    host_c = next((h for h in hosts if h["host"] == "host-c"), None)
    assert host_c is not None, hosts
    # The new column is plumbed end-to-end.
    assert host_c.get("disk_total_mb") == 500_000
    assert host_c.get("disk_free_mb") == 200_000
