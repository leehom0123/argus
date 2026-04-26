"""Status / activity guards on destructive endpoints (v0.1.3).

Goal: refuse to soft-delete a row that is still actively reporting,
since the reporter would otherwise keep writing to a row whose
``is_deleted=True`` makes it invisible to every UI surface.

Coverage:

* ``DELETE /api/batches/{id}``  — 409 when status ∈ {running, pending,
  stopping}.
* ``POST /api/batches/bulk-delete`` — partition active batches into
  ``skipped`` instead of failing the whole call.
* ``DELETE /api/jobs/{batch}/{job}`` — 409 for running / pending jobs.
* ``POST /api/jobs/bulk-delete`` — same partition pattern.
* ``POST /api/admin/hosts/bulk-delete`` — 409 / skip when the host
  has reported a resource snapshot in the last 10 minutes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.tests._dashboard_helpers import (
    post_event,
    seed_completed_batch,
)


def _start_event(batch_id: str, project: str = "guard") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-25T08:00:00Z",
        "batch_id": batch_id,
        "source": {"project": project},
        "data": {"n_total_jobs": 1},
    }


def _job_start(batch_id: str, job_id: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_start",
        "timestamp": "2026-04-25T08:01:00Z",
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": "guard"},
        "data": {"model": "transformer", "dataset": "etth1"},
    }


async def _terminate_batch(client, batch_id: str) -> None:
    """Post a batch_done event so the parent batch lands in a terminal
    status and the delete guard lets it through.

    ``seed_completed_batch`` only writes the per-job done event; the
    parent ``Batch.status`` stays 'running' until something explicitly
    fires _handle_batch_done.
    """
    ev = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_done",
        "timestamp": "2026-04-25T08:05:00Z",
        "batch_id": batch_id,
        "source": {"project": "guard"},
        "data": {"n_done": 1, "n_failed": 0, "total_elapsed_s": 30},
    }
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Batch delete guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_running_batch_returns_409(client):
    """DELETE on a running batch must 409, not soft-delete it."""
    await client.post("/api/events", json=_start_event("g-run-1"))

    r = await client.delete("/api/batches/g-run-1")
    assert r.status_code == 409, r.text

    # Batch must still be visible (NOT soft-deleted).
    r2 = await client.get("/api/batches/g-run-1")
    assert r2.status_code == 200
    assert r2.json()["status"] == "running"


@pytest.mark.asyncio
async def test_delete_stopping_batch_still_blocked(client):
    """A stop-then-delete race must still 409 while the reporter
    hasn't finished the cooperative shutdown.
    """
    await client.post("/api/events", json=_start_event("g-stop-1"))

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    # Stop succeeds: status flips to 'stopping'.
    r_stop = await client.post(
        "/api/batches/g-stop-1/stop",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r_stop.status_code == 200
    assert r_stop.json()["status"] == "stopping"

    # Delete must still fail until the reporter flips the row to a
    # terminal status (done / failed / stopped).
    r = await client.delete("/api/batches/g-stop-1")
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_delete_completed_batch_succeeds(client):
    """A done batch can still be deleted normally — guard only blocks
    active states."""
    await seed_completed_batch(
        client, batch_id="g-done-1", project="guard"
    )
    await _terminate_batch(client, "g-done-1")

    r = await client.delete("/api/batches/g-done-1")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_bulk_delete_partitions_running_into_skipped(client):
    """Mixing a running batch and a done batch in one bulk call:
    the running one goes to ``skipped`` with reason ``running``;
    the done one is deleted; the call still returns 200.
    """
    await client.post("/api/events", json=_start_event("g-mix-run"))
    await seed_completed_batch(
        client, batch_id="g-mix-done", project="guard"
    )
    await _terminate_batch(client, "g-mix-done")

    r = await client.post(
        "/api/batches/bulk-delete",
        json={"batch_ids": ["g-mix-run", "g-mix-done"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["deleted"] == ["g-mix-done"]
    skipped_ids = {s["id"]: s["reason"] for s in body["skipped"]}
    assert skipped_ids == {"g-mix-run": "running"}, skipped_ids


# ---------------------------------------------------------------------------
# Job delete guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_running_job_returns_409(client):
    """DELETE on a running job must 409."""
    await client.post("/api/events", json=_start_event("g-jrun-1"))
    await client.post(
        "/api/events", json=_job_start("g-jrun-1", "g-jrun-1-job-0")
    )

    r = await client.delete("/api/jobs/g-jrun-1/g-jrun-1-job-0")
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_bulk_delete_jobs_partitions_running(client):
    """Mixed bulk job delete: running → skipped, done → deleted."""
    # Running job
    await client.post("/api/events", json=_start_event("g-jbulk-r"))
    await client.post(
        "/api/events", json=_job_start("g-jbulk-r", "g-jbulk-r-job-0")
    )

    # Completed batch+job (done status)
    await seed_completed_batch(
        client, batch_id="g-jbulk-d", project="guard"
    )

    r = await client.post(
        "/api/jobs/bulk-delete",
        json={
            "items": [
                {"batch_id": "g-jbulk-r", "job_id": "g-jbulk-r-job-0"},
                {"batch_id": "g-jbulk-d", "job_id": "g-jbulk-d-job-0"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "g-jbulk-d/g-jbulk-d-job-0" in body["deleted"]
    skipped = {s["id"]: s["reason"] for s in body["skipped"]}
    assert "g-jbulk-r/g-jbulk-r-job-0" in skipped
    assert skipped["g-jbulk-r/g-jbulk-r-job-0"] == "running"


# ---------------------------------------------------------------------------
# Host delete guard (resource_snapshot recency)
# ---------------------------------------------------------------------------


def _utcnow_iso(offset_minutes: int = 0) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(minutes=offset_minutes))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _resource_snapshot(host: str, ts: str, batch_id: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "resource_snapshot",
        "timestamp": ts,
        "batch_id": batch_id,
        "source": {"project": "guard", "host": host},
        "data": {
            "gpu_util_pct": 50,
            "gpu_mem_mb": 4000,
            "gpu_mem_total_mb": 24000,
            "cpu_util_pct": 30,
            "ram_mb": 8000,
            "ram_total_mb": 64000,
            "disk_free_mb": 100_000,
        },
    }


@pytest.mark.asyncio
async def test_delete_active_host_skipped(client):
    """Host with a snapshot in the last 10 min → routed to ``skipped``."""
    # A reporting batch on host 'hot-host'.
    ev = _start_event("g-host-1")
    ev["source"]["host"] = "hot-host"
    await client.post("/api/events", json=ev)
    # Snapshot timestamped NOW — well within the 10-min cutoff.
    await client.post(
        "/api/events",
        json=_resource_snapshot("hot-host", _utcnow_iso(0), "g-host-1"),
    )

    r = await client.post(
        "/api/admin/hosts/bulk-delete",
        json={"hosts": ["hot-host"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == []
    skipped = {s["id"]: s["reason"] for s in body["skipped"]}
    assert skipped == {"hot-host": "active"}, skipped


@pytest.mark.asyncio
async def test_delete_idle_host_succeeds(client):
    """Host whose latest snapshot is older than 10 min → deleted normally."""
    ev = _start_event("g-host-2")
    ev["source"]["host"] = "cold-host"
    await client.post("/api/events", json=ev)
    # 30-minute-old snapshot — well outside the cutoff.
    await client.post(
        "/api/events",
        json=_resource_snapshot("cold-host", _utcnow_iso(30), "g-host-2"),
    )

    r = await client.post(
        "/api/admin/hosts/bulk-delete",
        json={"hosts": ["cold-host"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == ["cold-host"]
    assert body["skipped"] == []


@pytest.mark.asyncio
async def test_delete_unknown_host_succeeds(client):
    """Host with no snapshots at all → deleted normally (no false positive)."""
    r = await client.post(
        "/api/admin/hosts/bulk-delete",
        json={"hosts": ["never-seen-host"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == ["never-seen-host"]
