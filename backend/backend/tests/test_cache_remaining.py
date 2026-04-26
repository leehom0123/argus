"""TTL-cache coverage for the remaining hot read endpoints.

Wrapping these with ``default_cache`` was the Part-1 half of the /batches
fan-out fix: even after the bulk compact endpoint, the Dashboard still
fans out to projects-list + gpu-hours + notifications on every render.
Each of those now shares the same 10s TTL cache as the batch endpoints.

These tests assert the second-call-within-TTL contract plus the write-
path cache-bust for ``mark_all_read`` / ``ack`` / ``delete`` on
notifications.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from backend.utils.response_cache import default_cache as _response_cache


# ---------------------------------------------------------------------------
# list_projects — key prefix ``projects-list:``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects_second_call_hits_cache(client, monkeypatch):
    # Seed at least one event so the projects list has something to return.
    await client.post("/api/events", json={
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-24T09:00:00Z",
        "batch_id": "b-proj-1",
        "source": {"project": "proj_cache_a", "user": "tester"},
        "data": {"n_total_jobs": 1},
    })
    _response_cache.clear()

    load_count = 0
    orig = _response_cache.get_or_compute

    async def counting(key, loader):
        if key.startswith("projects-list:"):
            async def wrapped():
                nonlocal load_count
                load_count += 1
                return await loader()
            return await orig(key, wrapped)
        return await orig(key, loader)

    monkeypatch.setattr(_response_cache, "get_or_compute", counting)

    r1 = await client.get("/api/projects")
    r2 = await client.get("/api/projects")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert load_count == 1, f"expected 1 loader run for projects-list, got {load_count}"


# ---------------------------------------------------------------------------
# gpu-hours-by-user — key prefix ``gpu-hours:``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpu_hours_second_call_hits_cache(client, monkeypatch):
    _response_cache.clear()
    load_count = 0
    orig = _response_cache.get_or_compute

    async def counting(key, loader):
        if key.startswith("gpu-hours:"):
            async def wrapped():
                nonlocal load_count
                load_count += 1
                return await loader()
            return await orig(key, wrapped)
        return await orig(key, loader)

    monkeypatch.setattr(_response_cache, "get_or_compute", counting)

    r1 = await client.get("/api/stats/gpu-hours-by-user?days=30")
    r2 = await client.get("/api/stats/gpu-hours-by-user?days=30")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert load_count == 1, f"expected 1 loader run for gpu-hours, got {load_count}"


@pytest.mark.asyncio
async def test_gpu_hours_different_days_key_does_not_collide(client, monkeypatch):
    """days=30 vs days=7 hit separate cache keys → 2 loader runs."""
    _response_cache.clear()
    seen_keys: list[str] = []
    orig = _response_cache.get_or_compute

    async def spying(key, loader):
        if key.startswith("gpu-hours:"):
            seen_keys.append(key)
        return await orig(key, loader)

    monkeypatch.setattr(_response_cache, "get_or_compute", spying)

    await client.get("/api/stats/gpu-hours-by-user?days=30")
    await client.get("/api/stats/gpu-hours-by-user?days=7")
    assert len(seen_keys) == 2, seen_keys
    assert len(set(seen_keys)) == 2, seen_keys


# ---------------------------------------------------------------------------
# list_notifications — key prefix ``notifications:``
# ---------------------------------------------------------------------------


async def _seed_notification(client) -> int:
    """Insert a notification row for the default ``tester`` user, return id."""
    from backend.db import SessionLocal
    from backend.models import Notification

    # Get user id.
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    user_id = me.json()["id"]

    now = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    async with SessionLocal() as db:
        row = Notification(
            user_id=user_id,
            batch_id="b-cache-n",
            rule_id="val_loss_diverging",
            severity="warn",
            title="Cache test alert",
            body="Divergence detected.",
            created_at=now,
            read_at=None,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


@pytest.mark.asyncio
async def test_list_notifications_second_call_hits_cache(client, monkeypatch):
    await _seed_notification(client)
    _response_cache.clear()

    load_count = 0
    orig = _response_cache.get_or_compute

    async def counting(key, loader):
        if key.startswith("notifications:"):
            async def wrapped():
                nonlocal load_count
                load_count += 1
                return await loader()
            return await orig(key, wrapped)
        return await orig(key, loader)

    monkeypatch.setattr(_response_cache, "get_or_compute", counting)

    r1 = await client.get("/api/notifications")
    r2 = await client.get("/api/notifications")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert load_count == 1


@pytest.mark.asyncio
async def test_notifications_mark_read_busts_cache(client):
    """After POST /{id}/ack the next GET must reflect read_at != None."""
    nid = await _seed_notification(client)
    _response_cache.clear()

    r1 = await client.get("/api/notifications")
    assert r1.status_code == 200
    before = r1.json()
    assert len(before) >= 1
    assert any(n["id"] == nid and n["read_at"] is None for n in before)

    ack = await client.post(f"/api/notifications/{nid}/ack")
    assert ack.status_code == 204

    # Second GET should NOT serve the stale ``read_at=None`` from the
    # pre-ack cache entry — the ack handler busts the prefix.
    r2 = await client.get("/api/notifications")
    assert r2.status_code == 200
    after = r2.json()
    matching = [n for n in after if n["id"] == nid]
    assert len(matching) == 1
    assert matching[0]["read_at"] is not None, (
        "mark-read cache bust failed — read_at still None after ack"
    )


@pytest.mark.asyncio
async def test_notifications_mark_all_read_busts_cache(client):
    """Same contract for POST /mark_all_read."""
    nid = await _seed_notification(client)
    _response_cache.clear()

    r1 = await client.get("/api/notifications")
    assert r1.status_code == 200

    ack = await client.post("/api/notifications/mark_all_read")
    assert ack.status_code == 204

    r2 = await client.get("/api/notifications")
    assert r2.status_code == 200
    matching = [n for n in r2.json() if n["id"] == nid]
    assert len(matching) == 1
    assert matching[0]["read_at"] is not None
