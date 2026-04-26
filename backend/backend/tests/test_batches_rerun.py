"""Tests for POST /batches/{id}/rerun and GET /batches/{id}/rerun-info.

PM roadmap #5 — "Rerun with overrides".
"""
from __future__ import annotations

import json
import uuid

import pytest


def _batch_start_event(batch_id: str, n_total: int = 2) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-24T10:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "proj"},
        "data": {"n_total_jobs": n_total, "command": "main.py experiment=x"},
    }


def _batch_done_event(batch_id: str) -> dict:
    """Mark a batch as terminal so the Executor's source-state guard
    (#103 v0.1.5) accepts it for rerun. The legacy "fake rerun" route
    had no guard — these helper events bring the test scenarios in
    line with the real lifecycle."""
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_done",
        "timestamp": "2026-04-24T10:05:00Z",
        "batch_id": batch_id,
        "source": {"project": "proj"},
        "data": {"n_done": 2, "n_failed": 0, "total_elapsed_s": 60},
    }


async def _seed_terminal_batch(client, batch_id: str, n_total: int = 2) -> None:
    """Common fixture: emit batch_start + batch_done so the source row
    is in a state that allows rerun (the Executor refuses ``running``)."""
    r = await client.post("/api/events", json=_batch_start_event(batch_id, n_total))
    assert r.status_code == 200, r.text
    r = await client.post("/api/events", json=_batch_done_event(batch_id))
    assert r.status_code == 200, r.text


async def _register_login(client, username: str) -> str:
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
    )
    return login.json()["access_token"]


@pytest.mark.asyncio
async def test_rerun_happy_path_creates_linked_batch(client):
    """Owner can rerun a batch; new batch row has correct linkage + event."""
    await _seed_terminal_batch(client, "src-batch-1")

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.post(
        "/api/batches/src-batch-1/rerun",
        json={"overrides": {"model.d_model": 256, "optimizer.lr": 0.0005}},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_batch_id"] == "src-batch-1"
    assert body["status"] == "requested"
    assert body["batch_id"].startswith("rerun-")
    assert body["name"].endswith("(rerun)")

    new_id = body["batch_id"]

    # The newly-created batch should be fetchable and carry source_batch_id
    r = await client.get(
        f"/api/batches/{new_id}",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["source_batch_id"] == "src-batch-1"
    assert detail["status"] == "requested"
    assert detail["project"] == "proj"

    # The rerun-info endpoint on the NEW id should echo the overrides
    r = await client.get(
        f"/api/batches/{new_id}/rerun-info",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["source_batch_id"] == "src-batch-1"
    assert info["requested_by"] == "tester"
    overrides = json.loads(info["overrides_json"])
    assert overrides == {"model.d_model": 256, "optimizer.lr": 0.0005}


@pytest.mark.asyncio
async def test_rerun_custom_name_respected(client):
    """Caller-supplied ``name`` overrides the default '… (rerun)' pattern."""
    await _seed_terminal_batch(client, "src-batch-2")

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.post(
        "/api/batches/src-batch-2/rerun",
        json={"overrides": {}, "name": "my-tuned-run"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "my-tuned-run"


@pytest.mark.asyncio
async def test_rerun_non_owner_forbidden(client):
    """Non-owner + non-admin gets 403 when reruning someone else's batch."""
    await _seed_terminal_batch(client, "src-batch-3")

    alice_jwt = await _register_login(client, "alice")
    r = await client.post(
        "/api/batches/src-batch-3/rerun",
        json={"overrides": {"lr": 0.001}},
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_rerun_unknown_batch_404(client):
    r = await client.post(
        "/api/batches/does-not-exist/rerun",
        json={"overrides": {}},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_rerun_info_returns_nulls_for_non_rerun_batch(client):
    """An ordinary batch with no source_batch_id reports every field null."""
    r = await client.post("/api/events", json=_batch_start_event("plain-batch"))
    assert r.status_code == 200

    r = await client.get("/api/batches/plain-batch/rerun-info")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "source_batch_id": None,
        "overrides_json": None,
        "requested_at": None,
        "requested_by": None,
    }


@pytest.mark.asyncio
async def test_rerun_event_is_persisted(client):
    """A rerun_requested Event row lives on the child batch for reporters."""
    await _seed_terminal_batch(client, "src-batch-4")

    r = await client.post(
        "/api/batches/src-batch-4/rerun",
        json={"overrides": {"training.epochs": 100}},
    )
    assert r.status_code == 201
    new_id = r.json()["batch_id"]

    # Reach into the child's rerun-info twice — idempotent, same payload.
    r1 = await client.get(f"/api/batches/{new_id}/rerun-info")
    r2 = await client.get(f"/api/batches/{new_id}/rerun-info")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert json.loads(r1.json()["overrides_json"]) == {"training.epochs": 100}
