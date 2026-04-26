"""Tests for ``GET /api/batches/compact`` — the bulk BatchCompactCard payload.

The compact endpoint replaces the ``/batches`` page's N×4 per-card fan-out
(get_batch + list_batch_jobs + epochs/latest + resources) with one bulk
call. These tests cover:

1. Shape: each BatchCompactItem carries the batch + its jobs + its
   epochs_latest + its resources, grouped correctly per batch_id.
2. Visibility: the ``include_demo=False`` default excludes demo-project
   batches; a visibility-filtered user only sees their own + shared
   batches (inherits from ``VisibilityResolver``).
3. resource_limit caps the snapshots per batch.
4. Response is cached — the second call inside the TTL window does not
   re-fire the loader.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.utils.response_cache import default_cache as _response_cache


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed_batch_full(
    client,
    batch_id: str,
    *,
    project: str = "p",
    n_jobs: int = 2,
    n_epochs_per_job: int = 3,
    n_snapshots: int = 5,
) -> None:
    """Seed a batch + N jobs + per-job epochs + per-batch resource snapshots."""
    base = datetime(2026, 4, 24, 9, 0, tzinfo=timezone.utc)
    src = {"project": project, "host": "h1", "user": "tester"}

    # batch_start
    await client.post("/api/events", json={
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": _iso(base),
        "batch_id": batch_id,
        "source": src,
        "data": {"n_total_jobs": n_jobs},
    })

    for i in range(n_jobs):
        job_id = f"{batch_id}-job-{i}"
        # job_start
        await client.post("/api/events", json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_start",
            "timestamp": _iso(base + timedelta(seconds=10 * (i + 1))),
            "batch_id": batch_id,
            "job_id": job_id,
            "source": src,
            "data": {"model": "transformer", "dataset": "etth1"},
        })
        # n_epochs_per_job job_epoch rows per job
        for e in range(n_epochs_per_job):
            await client.post("/api/events", json={
                "event_id": str(uuid.uuid4()),
                "schema_version": "1.1",
                "event_type": "job_epoch",
                "timestamp": _iso(
                    base + timedelta(seconds=10 * (i + 1) + e * 2)
                ),
                "batch_id": batch_id,
                "job_id": job_id,
                "source": src,
                "data": {
                    "epoch": e + 1,
                    "train_loss": 0.5 - e * 0.1,
                    "val_loss": 0.6 - e * 0.05,
                    "lr": 1e-4,
                },
            })

    # n_snapshots resource_snapshots for the batch host
    for k in range(n_snapshots):
        await client.post("/api/events", json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "resource_snapshot",
            "timestamp": _iso(base + timedelta(seconds=60 + k * 5)),
            "batch_id": batch_id,
            "source": src,
            "data": {
                "gpu_util_pct": 50 + k,
                "gpu_mem_mb": 1000 + k * 100,
                "gpu_mem_total_mb": 24000,
                "cpu_util_pct": 30,
                "ram_mb": 4000,
                "ram_total_mb": 64000,
            },
        })


@pytest.mark.asyncio
async def test_compact_shape_bundles_everything(client):
    """A single batch's compact item carries batch + jobs + epochs + resources."""
    await _seed_batch_full(
        client, "b-compact-1", n_jobs=2, n_epochs_per_job=3, n_snapshots=4,
    )

    r = await client.get("/api/batches/compact")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)
    assert "batches" in body
    items = body["batches"]
    # We seeded one batch; find the one we care about (tester is admin
    # in this suite so scope=all is default).
    ours = [it for it in items if it["batch"]["id"] == "b-compact-1"]
    assert len(ours) == 1, f"expected b-compact-1 in payload, got {items}"
    it = ours[0]

    # batch section
    assert it["batch"]["id"] == "b-compact-1"
    # jobs section: 2 jobs
    assert len(it["jobs"]) == 2
    job_ids = {j["id"] for j in it["jobs"]}
    assert job_ids == {"b-compact-1-job-0", "b-compact-1-job-1"}
    # epochs_latest: one entry per job, latest epoch = 3, trace len 3
    assert len(it["epochs_latest"]) == 2
    for ep in it["epochs_latest"]:
        assert ep["epoch"] == 3
        # trace accumulated chronologically; last val ≈ 0.5
        assert len(ep["val_loss_trace"]) == 3
        assert ep["val_loss_trace"][-1] == pytest.approx(0.5)
    # resources: 4 snapshots (no cap hit)
    assert len(it["resources"]) == 4
    # newest first (timestamp DESC)
    ts = [s["timestamp"] for s in it["resources"]]
    assert ts == sorted(ts, reverse=True)


@pytest.mark.asyncio
async def test_compact_resource_limit_caps_per_batch(client):
    """resource_limit=2 caps each batch's resources[] to 2 newest."""
    await _seed_batch_full(
        client, "b-compact-lim", n_jobs=1, n_epochs_per_job=1, n_snapshots=5,
    )

    r = await client.get("/api/batches/compact?resource_limit=2")
    assert r.status_code == 200, r.text
    ours = [it for it in r.json()["batches"] if it["batch"]["id"] == "b-compact-lim"]
    assert len(ours) == 1
    assert len(ours[0]["resources"]) == 2


@pytest.mark.asyncio
async def test_compact_groups_per_batch(client):
    """Jobs / epochs / resources MUST stay scoped to their parent batch."""
    await _seed_batch_full(
        client, "b-compact-A", n_jobs=1, n_epochs_per_job=2, n_snapshots=1,
    )
    await _seed_batch_full(
        client, "b-compact-B", n_jobs=2, n_epochs_per_job=1, n_snapshots=3,
    )

    r = await client.get("/api/batches/compact")
    assert r.status_code == 200
    items = {it["batch"]["id"]: it for it in r.json()["batches"]}
    assert "b-compact-A" in items and "b-compact-B" in items

    a = items["b-compact-A"]
    b = items["b-compact-B"]
    assert len(a["jobs"]) == 1 and len(b["jobs"]) == 2
    assert len(a["epochs_latest"]) == 1 and len(b["epochs_latest"]) == 2
    assert len(a["resources"]) == 1 and len(b["resources"]) == 3

    # Cross-pollination check: no job from A in B, or vice versa.
    a_job_batches = {j["batch_id"] for j in a["jobs"]}
    b_job_batches = {j["batch_id"] for j in b["jobs"]}
    assert a_job_batches == {"b-compact-A"}
    assert b_job_batches == {"b-compact-B"}


@pytest.mark.asyncio
async def test_compact_second_call_hits_cache(client, monkeypatch):
    """Second compact call inside TTL → loader runs once total."""
    await _seed_batch_full(
        client, "b-compact-cache", n_jobs=1, n_epochs_per_job=1, n_snapshots=1,
    )
    _response_cache.clear()

    load_count = 0
    orig = _response_cache.get_or_compute

    async def counting(key, loader):
        if key.startswith("batches-compact:"):
            async def wrapped():
                nonlocal load_count
                load_count += 1
                return await loader()
            return await orig(key, wrapped)
        return await orig(key, loader)

    monkeypatch.setattr(_response_cache, "get_or_compute", counting)

    r1 = await client.get("/api/batches/compact")
    assert r1.status_code == 200
    r2 = await client.get("/api/batches/compact")
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert load_count == 1, f"expected 1 loader run, got {load_count}"


@pytest.mark.asyncio
async def test_compact_empty_when_no_batches(client):
    """No visible batches → empty ``batches`` list (not 404 / not null)."""
    r = await client.get("/api/batches/compact?status=does_not_exist")
    assert r.status_code == 200
    body = r.json()
    assert body == {"batches": []}


@pytest.mark.asyncio
async def test_compact_respects_visibility_for_non_admin(client):
    """A second user without any share sees an empty ``batches`` list even
    when the first user has seeded rows."""
    await _seed_batch_full(
        client, "b-compact-priv", n_jobs=1, n_epochs_per_job=1, n_snapshots=1,
    )
    # Register bob (non-admin, not a sharee) and use his JWT.
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": "bob_compact",
            "email": "bob_compact@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": "bob_compact", "password": "password123"},
    )
    bob_jwt = lr.json()["access_token"]

    r = await client.get(
        "/api/batches/compact",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 200
    items = r.json()["batches"]
    ids = {it["batch"]["id"] for it in items}
    assert "b-compact-priv" not in ids, (
        f"non-admin bob should not see tester's batch; got {ids}"
    )
