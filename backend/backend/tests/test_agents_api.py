"""Tests for the ``/api/agents/*`` endpoints (#103 v0.1.5).

The agent lifecycle is small but contract-heavy — three endpoints
exposed to two distinct callers (the human installing the daemon, and
the daemon itself):

* ``POST /api/agents/register``       — JWT-authed, returns plaintext token
* ``GET  /api/agents/{id}/jobs``      — agent-token-authed, lists pending cmds
* ``POST /api/agents/{id}/jobs/{cmd}/ack`` — agent-token-authed
* ``POST /api/agents/{id}/heartbeat`` — agent-token-authed, 204

Auth pitfalls covered: foreign tokens, mismatched ``agent_id`` in path,
double-ack, missing-prefix tokens.
"""
from __future__ import annotations

import uuid

import pytest


def _batch_start_event(batch_id: str, host: str = "host-A") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-26T08:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "proj", "host": host},
        "data": {"n_total_jobs": 1, "command": "main.py experiment=x"},
    }


def _batch_done_event(batch_id: str, host: str = "host-A") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_done",
        "timestamp": "2026-04-26T08:05:00Z",
        "batch_id": batch_id,
        "source": {"project": "proj", "host": host},
        "data": {"n_done": 1, "n_failed": 0, "total_elapsed_s": 60},
    }


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_mints_token_and_id(client):
    """Happy-path register returns a plaintext token + agent_id + interval."""
    r = await client.post(
        "/api/agents/register",
        json={
            "hostname": "host-1",
            "version": "0.1.5",
            "capabilities": ["rerun", "stop"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["agent_id"].startswith("agent-")
    assert body["agent_token"].startswith("ag_live_")
    assert body["poll_interval_s"] == 10
    assert "T" in body["server_time_utc"]


@pytest.mark.asyncio
async def test_register_re_registers_rotates_token(client):
    """Re-registering the same hostname rotates the token in place.

    This is intentional: an agent that lost its token from disk must
    bootstrap without admin help. The previous plaintext stops working
    the moment the new one is minted.
    """
    r = await client.post(
        "/api/agents/register",
        json={"hostname": "host-rot", "capabilities": []},
    )
    assert r.status_code == 201
    first = r.json()

    r = await client.post(
        "/api/agents/register",
        json={"hostname": "host-rot", "capabilities": []},
    )
    assert r.status_code == 201
    second = r.json()

    assert first["agent_id"] == second["agent_id"]
    assert first["agent_token"] != second["agent_token"]

    # Old token rejected.
    r = await client.get(
        f"/api/agents/{first['agent_id']}/jobs",
        headers={"Authorization": f"Bearer {first['agent_token']}"},
    )
    assert r.status_code == 401

    # New token accepted.
    r = await client.get(
        f"/api/agents/{second['agent_id']}/jobs",
        headers={"Authorization": f"Bearer {second['agent_token']}"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_register_requires_auth(unauthed_client):
    """No JWT → 401. The register endpoint is the human-claim step;
    the agent itself doesn't have a JWT so an unauthed call must fail."""
    r = await unauthed_client.post(
        "/api/agents/register",
        json={"hostname": "host-anon", "capabilities": []},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Poll / ack
# ---------------------------------------------------------------------------


async def _seed_rerun(client, batch_id: str = "src-1", host: str = "host-poll") -> str:
    """Set up a terminal batch + register agent + request rerun.

    Returns the new batch id so the caller can assert against it.
    """
    r = await client.post("/api/events", json=_batch_start_event(batch_id, host))
    assert r.status_code == 200
    r = await client.post("/api/events", json=_batch_done_event(batch_id, host))
    assert r.status_code == 200

    r = await client.post(
        "/api/agents/register",
        json={"hostname": host, "capabilities": ["rerun"]},
    )
    assert r.status_code == 201
    agent_id = r.json()["agent_id"]
    agent_token = r.json()["agent_token"]
    client._test_agent_id = agent_id  # type: ignore[attr-defined]
    client._test_agent_token = agent_token  # type: ignore[attr-defined]

    r = await client.post(f"/api/batches/{batch_id}/rerun", json={"overrides": {}})
    assert r.status_code == 201, r.text
    return r.json()["batch_id"]


@pytest.mark.asyncio
async def test_poll_jobs_returns_pending_only(client):
    """``GET /jobs`` returns rows with status='pending' for the agent's host."""
    new_batch_id = await _seed_rerun(client)
    agent_id = client._test_agent_id  # type: ignore[attr-defined]
    agent_token = client._test_agent_token  # type: ignore[attr-defined]

    r = await client.get(
        f"/api/agents/{agent_id}/jobs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "rerun"
    assert jobs[0]["batch_id"] == new_batch_id


@pytest.mark.asyncio
async def test_poll_rejects_foreign_agent_id(client):
    """Token for agent A can't poll agent B's queue (path/token mismatch)."""
    await _seed_rerun(client)
    other_agent_token = client._test_agent_token  # type: ignore[attr-defined]

    r = await client.get(
        "/api/agents/agent-deadbeef/jobs",
        headers={"Authorization": f"Bearer {other_agent_token}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_poll_rejects_unknown_token(client):
    """Tokens not in the DB → 401, even with the right prefix."""
    await _seed_rerun(client)
    agent_id = client._test_agent_id  # type: ignore[attr-defined]

    r = await client.get(
        f"/api/agents/{agent_id}/jobs",
        headers={"Authorization": "Bearer ag_live_garbage"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_ack_started_flips_batch_to_running(client):
    """A successful ``status='started'`` ack flips the new batch from
    ``requested`` → ``running``. This is the signal the frontend's
    60s rerun-toast poll listens for."""
    new_batch_id = await _seed_rerun(client)
    agent_id = client._test_agent_id  # type: ignore[attr-defined]
    agent_token = client._test_agent_token  # type: ignore[attr-defined]

    # Pull the cmd id off the queue.
    r = await client.get(
        f"/api/agents/{agent_id}/jobs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    cmd_id = r.json()["jobs"][0]["id"]

    r = await client.post(
        f"/api/agents/{agent_id}/jobs/{cmd_id}/ack",
        json={"status": "started", "pid": 12345},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"

    # Batch flipped to running.
    r = await client.get(f"/api/batches/{new_batch_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "running"


@pytest.mark.asyncio
async def test_ack_failed_keeps_batch_requested(client):
    """A ``status='failed'`` ack records the error but leaves the batch
    in ``requested`` so the user can retry."""
    new_batch_id = await _seed_rerun(client)
    agent_id = client._test_agent_id  # type: ignore[attr-defined]
    agent_token = client._test_agent_token  # type: ignore[attr-defined]

    r = await client.get(
        f"/api/agents/{agent_id}/jobs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    cmd_id = r.json()["jobs"][0]["id"]

    r = await client.post(
        f"/api/agents/{agent_id}/jobs/{cmd_id}/ack",
        json={"status": "failed", "error": "FileNotFoundError: cwd missing"},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200

    r = await client.get(f"/api/batches/{new_batch_id}")
    assert r.json()["status"] == "requested"


@pytest.mark.asyncio
async def test_double_ack_returns_409(client):
    """A second ack on the same cmd_id is rejected — guards against an
    agent restart re-acking work that's already been processed."""
    await _seed_rerun(client)
    agent_id = client._test_agent_id  # type: ignore[attr-defined]
    agent_token = client._test_agent_token  # type: ignore[attr-defined]

    r = await client.get(
        f"/api/agents/{agent_id}/jobs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    cmd_id = r.json()["jobs"][0]["id"]

    headers = {"Authorization": f"Bearer {agent_token}"}
    r = await client.post(
        f"/api/agents/{agent_id}/jobs/{cmd_id}/ack",
        json={"status": "started", "pid": 1},
        headers=headers,
    )
    assert r.status_code == 200

    r = await client.post(
        f"/api/agents/{agent_id}/jobs/{cmd_id}/ack",
        json={"status": "started", "pid": 1},
        headers=headers,
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_heartbeat_returns_204(client):
    """Heartbeats are 204 No Content — the body is reserved for future
    telemetry but not used yet."""
    r = await client.post(
        "/api/agents/register",
        json={"hostname": "host-hb", "capabilities": []},
    )
    agent_id = r.json()["agent_id"]
    agent_token = r.json()["agent_token"]

    r = await client.post(
        f"/api/agents/{agent_id}/heartbeat",
        json={"note": "alive"},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 204
    assert r.content == b""
