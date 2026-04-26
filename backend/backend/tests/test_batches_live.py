"""Integration tests for the 3 restored batch data-gap routes.

Routes under test:
  GET /api/batches/{id}/resources
  GET /api/batches/{id}/log-lines
  GET /api/batches/{id}/epochs/latest
"""
from __future__ import annotations

import uuid

import pytest

from backend.tests._dashboard_helpers import (
    mk_user_with_token,
    make_batch_start,
    make_job_start,
    post_event,
    seed_completed_batch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_resource_snapshot(
    host: str,
    batch_id: str,
    ts: str = "2026-04-23T09:01:30Z",
    include_proc: bool = False,
) -> dict:
    data: dict = {
        "gpu_util_pct": 72.5,
        "gpu_mem_mb": 4096,
        "gpu_mem_total_mb": 24576,
        "gpu_temp_c": 65,
        "cpu_util_pct": 30.0,
        "ram_mb": 8192,
        "ram_total_mb": 65536,
        "disk_free_mb": 204800,
    }
    if include_proc:
        data["proc_cpu_pct"] = 18.5
        data["proc_ram_mb"] = 1024
        data["proc_gpu_mem_mb"] = 2048
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "resource_snapshot",
        "timestamp": ts,
        "batch_id": batch_id,
        "source": {"project": "deepts", "host": host},
        "data": data,
    }


def _make_log_line(
    batch_id: str,
    job_id: str | None = None,
    level: str = "info",
    line: str = "training step 1",
    ts: str = "2026-04-23T09:01:45Z",
) -> dict:
    ev: dict = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "log_line",
        "timestamp": ts,
        "batch_id": batch_id,
        "source": {"project": "deepts"},
        "data": {"level": level, "line": line},
    }
    if job_id is not None:
        ev["job_id"] = job_id
    return ev


def _make_job_epoch(
    batch_id: str,
    job_id: str,
    epoch: int,
    train_loss: float = 0.5,
    val_loss: float = 0.55,
    lr: float = 1e-4,
    ts: str = "2026-04-23T09:01:10Z",
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_epoch",
        "timestamp": ts,
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": "deepts"},
        "data": {
            "epoch": epoch,
            "train_loss": train_loss,
            "val_loss": val_loss,
            "lr": lr,
        },
    }


# ---------------------------------------------------------------------------
# GET /api/batches/{id}/resources
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resources_200_shape(client):
    """Authenticated user + valid batch → 200 with correct shape."""
    host = "lab-gpu-01"
    batch_id = "res-batch-1"

    # seed batch with a host
    await post_event(
        client,
        {**make_batch_start(batch_id, host=host), "event_id": str(uuid.uuid4())},
    )
    # post one resource snapshot on that host
    await post_event(client, _make_resource_snapshot(host, batch_id))

    r = await client.get(f"/api/batches/{batch_id}/resources")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host"] == host
    assert isinstance(body["snapshots"], list)
    assert len(body["snapshots"]) == 1
    snap = body["snapshots"][0]
    assert "ts" in snap
    assert "gpu_util" in snap
    assert "vram_used_mb" in snap
    assert "vram_total_mb" in snap
    assert "cpu_util" in snap
    assert "ram_used_mb" in snap
    assert "ram_total_mb" in snap
    assert "disk_free_gb" in snap
    # disk_free_mb=204800 → disk_free_gb=200.0
    assert snap["disk_free_gb"] == pytest.approx(200.0, rel=0.01)


@pytest.mark.asyncio
async def test_resources_404_invalid_batch(client):
    """Unknown batch_id → 404 with translated detail."""
    r = await client.get(
        "/api/batches/no-such-batch/resources",
        headers={"Accept-Language": "zh-CN"},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "未找到该批次"


# ---------------------------------------------------------------------------
# GET /api/batches/{id}/log-lines
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_lines_200_shape(client):
    """Authenticated user + valid batch → 200 with correct shape."""
    batch_id = "log-batch-1"
    job_id = "j-log-1"

    await post_event(
        client,
        {**make_batch_start(batch_id), "event_id": str(uuid.uuid4())},
    )
    await post_event(
        client,
        _make_log_line(batch_id, job_id=job_id, level="warning", line="loss spike"),
    )

    r = await client.get(f"/api/batches/{batch_id}/log-lines")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    row = body[0]
    assert "ts" in row
    assert row["job_id"] == job_id
    assert row["level"] == "warning"
    assert row["line"] == "loss spike"


@pytest.mark.asyncio
async def test_log_lines_job_id_filter(client):
    """job_id query param filters correctly."""
    batch_id = "log-batch-2"

    await post_event(
        client,
        {**make_batch_start(batch_id), "event_id": str(uuid.uuid4())},
    )
    # two log lines: one for j-a, one for j-b
    await post_event(
        client,
        _make_log_line(batch_id, job_id="j-a", line="msg from a", ts="2026-04-23T09:01:00Z"),
    )
    await post_event(
        client,
        _make_log_line(batch_id, job_id="j-b", line="msg from b", ts="2026-04-23T09:02:00Z"),
    )

    r = await client.get(f"/api/batches/{batch_id}/log-lines?job_id=j-a")
    assert r.status_code == 200
    body = r.json()
    assert all(row["job_id"] == "j-a" for row in body)
    assert len(body) == 1


@pytest.mark.asyncio
async def test_log_lines_404_zh_cn(client):
    """Unknown batch_id → 404 with zh-CN detail."""
    r = await client.get(
        "/api/batches/ghost-batch/log-lines",
        headers={"Accept-Language": "zh-CN"},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "未找到该批次"


# ---------------------------------------------------------------------------
# GET /api/batches/{id}/epochs/latest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_epochs_latest_200_shape(client):
    """Authenticated user + valid batch with epoch events → 200 correct shape."""
    batch_id = "ep-batch-1"
    job_id = "j-ep-1"

    await post_event(
        client,
        {**make_batch_start(batch_id), "event_id": str(uuid.uuid4())},
    )
    await post_event(client, make_job_start(batch_id, job_id))

    # Post 3 epochs
    for i in range(1, 4):
        await post_event(
            client,
            _make_job_epoch(
                batch_id, job_id, epoch=i,
                train_loss=0.5 - i * 0.05,
                val_loss=0.55 - i * 0.04,
                ts=f"2026-04-23T09:0{i}:00Z",
            ),
        )

    r = await client.get(f"/api/batches/{batch_id}/epochs/latest")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "jobs" in body
    assert len(body["jobs"]) == 1
    job = body["jobs"][0]
    assert job["job_id"] == job_id
    assert job["epoch"] == 3  # latest
    assert job["train_loss"] is not None
    assert job["val_loss"] is not None
    assert job["lr"] is not None
    trace = job["val_loss_trace"]
    assert isinstance(trace, list)
    # 3 epochs → 3 trace points
    assert len(trace) == 3


@pytest.mark.asyncio
async def test_epochs_latest_empty_no_epochs(client):
    """Batch with no epoch events → empty jobs list."""
    batch_id = "ep-batch-empty"
    await post_event(
        client,
        {**make_batch_start(batch_id), "event_id": str(uuid.uuid4())},
    )
    r = await client.get(f"/api/batches/{batch_id}/epochs/latest")
    assert r.status_code == 200
    assert r.json()["jobs"] == []


@pytest.mark.asyncio
async def test_epochs_latest_trace_capped_at_20(client):
    """val_loss_trace is capped at last 20 epochs even if more exist."""
    batch_id = "ep-batch-many"
    job_id = "j-many"

    await post_event(
        client,
        {**make_batch_start(batch_id), "event_id": str(uuid.uuid4())},
    )
    await post_event(client, make_job_start(batch_id, job_id))

    for i in range(1, 26):  # 25 epochs
        await post_event(
            client,
            _make_job_epoch(batch_id, job_id, epoch=i, val_loss=1.0 / i),
        )

    r = await client.get(f"/api/batches/{batch_id}/epochs/latest")
    assert r.status_code == 200
    job = r.json()["jobs"][0]
    assert len(job["val_loss_trace"]) == 20
    # last trace value should be from epoch 25 → val_loss = 1/25
    assert job["val_loss_trace"][-1] == pytest.approx(1.0 / 25, rel=0.01)


@pytest.mark.asyncio
async def test_epochs_latest_404_zh_cn(client):
    """Unknown batch_id → 404 with zh-CN detail."""
    r = await client.get(
        "/api/batches/no-batch/epochs/latest",
        headers={"Accept-Language": "zh-CN"},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "未找到该批次"


# ---------------------------------------------------------------------------
# GET /api/batches/{id}/resources — proc_* fields (migration 008)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resources_proc_fields_populated(client):
    """Snapshot carrying proc_* in data → /resources returns them populated."""
    host = "lab-gpu-proc-1"
    batch_id = "res-proc-batch-1"

    await post_event(
        client,
        {**make_batch_start(batch_id, host=host), "event_id": str(uuid.uuid4())},
    )
    await post_event(
        client,
        _make_resource_snapshot(host, batch_id, include_proc=True),
    )

    r = await client.get(f"/api/batches/{batch_id}/resources")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["snapshots"]) == 1
    snap = body["snapshots"][0]

    # New fields must be present in the response shape
    assert "proc_cpu_pct" in snap
    assert "proc_ram_mb" in snap
    assert "proc_gpu_mem_mb" in snap

    # Values must match what was sent
    assert snap["proc_cpu_pct"] == pytest.approx(18.5, rel=0.01)
    assert snap["proc_ram_mb"] == 1024
    assert snap["proc_gpu_mem_mb"] == 2048


@pytest.mark.asyncio
async def test_resources_proc_fields_null_for_legacy_snapshot(client):
    """Snapshot without proc_* fields → /resources returns them as null."""
    host = "lab-gpu-legacy-1"
    batch_id = "res-legacy-batch-1"

    await post_event(
        client,
        {**make_batch_start(batch_id, host=host), "event_id": str(uuid.uuid4())},
    )
    # include_proc=False → no proc_* keys in data
    await post_event(
        client,
        _make_resource_snapshot(host, batch_id, include_proc=False),
    )

    r = await client.get(f"/api/batches/{batch_id}/resources")
    assert r.status_code == 200, r.text
    snap = r.json()["snapshots"][0]

    # Fields must be present (for schema consistency) but null
    assert "proc_cpu_pct" in snap
    assert "proc_ram_mb" in snap
    assert "proc_gpu_mem_mb" in snap
    assert snap["proc_cpu_pct"] is None
    assert snap["proc_ram_mb"] is None
    assert snap["proc_gpu_mem_mb"] is None


@pytest.mark.asyncio
async def test_resources_batch_id_stored_on_snapshot(client):
    """batch_id from the event envelope is stored on the resource_snapshot row."""
    from sqlalchemy import select, text
    from backend.db import engine
    from backend.models import ResourceSnapshot

    host = "lab-gpu-batchid-1"
    batch_id = "res-batchid-batch-1"

    await post_event(
        client,
        {**make_batch_start(batch_id, host=host), "event_id": str(uuid.uuid4())},
    )
    await post_event(
        client,
        _make_resource_snapshot(host, batch_id, ts="2026-04-24T10:00:00Z"),
    )

    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(ResourceSnapshot.batch_id)
                .where(ResourceSnapshot.host == host)
                .order_by(ResourceSnapshot.timestamp.desc())
                .limit(1)
            )
        ).all()

    assert len(rows) == 1
    assert rows[0][0] == batch_id
