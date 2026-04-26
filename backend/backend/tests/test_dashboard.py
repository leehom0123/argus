"""Tests for ``GET /api/dashboard`` — home page aggregate."""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import (
    mk_user_with_token,
    post_event,
    seed_completed_batch,
    make_batch_start,
    make_resource_snapshot,
)


@pytest.mark.asyncio
async def test_dashboard_empty_state(client):
    """No data posted → all counters zero, lists empty."""
    r = await client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["scope"] == "all"
    assert body["counters"]["running_batches"] == 0
    assert body["counters"]["jobs_running"] == 0
    assert body["counters"]["jobs_done_24h"] == 0
    assert body["counters"]["my_running"] == 0
    assert body["projects"] == []
    assert body["activity"] == []
    assert "generated_at" in body


@pytest.mark.asyncio
async def test_dashboard_counts_running_batch(client):
    """One started-but-unfinished batch bumps running counters."""
    await post_event(
        client,
        make_batch_start("run-1", project="proj-a", n_total=4),
    )
    r = await client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    # Status starts as 'running' when set by batch_start; confirm it.
    assert body["counters"]["running_batches"] >= 1
    assert body["counters"]["my_running"] >= 1
    assert any(p["project"] == "proj-a" for p in body["projects"])


@pytest.mark.asyncio
async def test_dashboard_project_card_reflects_starred(client):
    """Starring a project → is_starred=True on that card."""
    await seed_completed_batch(client, batch_id="b-1", project="proj-star")
    # star it
    await client.post(
        "/api/stars",
        json={"target_type": "project", "target_id": "proj-star"},
    )
    r = await client.get("/api/dashboard")
    body = r.json()
    star_card = next(p for p in body["projects"] if p["project"] == "proj-star")
    assert star_card["is_starred"] is True


@pytest.mark.asyncio
async def test_dashboard_visibility_scopes_non_admin(client):
    """Bob (non-admin) only sees his own batch under scope=all."""
    # tester (admin) seeds a batch.
    await seed_completed_batch(client, batch_id="admin-only", project="admin-p")

    bob_jwt, bob_token = await mk_user_with_token(client, "bob")
    await post_event(
        client,
        make_batch_start("bob-only", project="bob-p", n_total=2),
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    r = await client.get(
        "/api/dashboard",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 200
    projects = [p["project"] for p in r.json()["projects"]]
    assert "bob-p" in projects
    assert "admin-p" not in projects


@pytest.mark.asyncio
async def test_dashboard_activity_feed_includes_batch_start(client):
    """batch_start appears in the activity feed with a summary string."""
    await post_event(
        client,
        make_batch_start("b-activity", project="p-activity", n_total=5),
    )
    r = await client.get("/api/dashboard")
    body = r.json()
    activity_types = [a["event_type"] for a in body["activity"]]
    assert "batch_start" in activity_types


@pytest.mark.asyncio
async def test_dashboard_requires_auth(unauthed_client):
    r = await unauthed_client.get("/api/dashboard")
    assert r.status_code == 401
