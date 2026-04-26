"""Tests for :class:`backend.services.executor.Executor` (#103 v0.1.5).

Covers the three contracts the architect required for the v0.1.5 slice:

1. ``request_rerun`` mints a new Batch row with ``source_batch_id`` set,
   writes the typed ``rerun_requested`` event, and enqueues an
   :class:`AgentCommand` when an agent is registered for the source host.
2. Source-state guard rejects reruns of running batches.
3. Idempotency window — re-clicking within 60 s reuses the existing
   pending command instead of minting a second clone.

Plus the agent-poll round-trip: the enqueued command shows up on
``GET /api/agents/{id}/jobs`` for the matching agent.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _batch_start_event(batch_id: str, host: str = "host-A") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-26T08:00:00Z",
        "batch_id": batch_id,
        # ``host`` is what AgentCommand routing matches on. Keep it
        # explicit so the dedup / agent-pickup tests are deterministic.
        "source": {"project": "proj", "host": host},
        "data": {"n_total_jobs": 2, "command": "main.py experiment=spike_v5"},
    }


def _batch_done_event(batch_id: str, host: str = "host-A") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_done",
        "timestamp": "2026-04-26T08:05:00Z",
        "batch_id": batch_id,
        "source": {"project": "proj", "host": host},
        "data": {"n_done": 2, "n_failed": 0, "total_elapsed_s": 60},
    }


async def _seed_terminal_batch(client, batch_id: str, host: str = "host-A") -> None:
    """Mark *batch_id* as ``done`` so the Executor's source-state guard accepts it."""
    r = await client.post("/api/events", json=_batch_start_event(batch_id, host))
    assert r.status_code == 200, r.text
    r = await client.post("/api/events", json=_batch_done_event(batch_id, host))
    assert r.status_code == 200, r.text


async def _register_agent(
    client, hostname: str, capabilities: list[str] | None = None
) -> tuple[str, str]:
    """Register an agent and return ``(agent_id, agent_token)``.

    Uses the default-fixture user's JWT so the registered agent's
    owner_id matches the one that creates batches via ``client``.
    """
    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/agents/register",
        json={
            "hostname": hostname,
            "version": "0.1.5",
            "capabilities": capabilities or ["rerun", "stop"],
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return body["agent_id"], body["agent_token"]


# ---------------------------------------------------------------------------
# Source-state guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_rejects_still_running_source(client):
    """Source in ``status='running'`` is refused with 409.

    Pre-Executor (legacy "fake rerun") would happily clone a running
    batch — the architect's design doc Section 5 idempotency table makes
    that an error so the user has to stop the live run first.
    """
    r = await client.post("/api/events", json=_batch_start_event("live-1"))
    assert r.status_code == 200

    r = await client.post("/api/batches/live-1/rerun", json={"overrides": {}})
    assert r.status_code == 409, r.text
    assert "running" in r.json()["detail"]


@pytest.mark.asyncio
async def test_rerun_accepts_failed_source(client):
    """Source in ``status='failed'`` is allowed (the canonical rerun case).

    Mirrors the user's original workflow: a training run crashes, they
    click Rerun on the broken batch, the Executor mints a fresh clone.
    """
    # Failed event — see backend/api/events.py::_handle_batch_failed.
    r = await client.post("/api/events", json=_batch_start_event("fail-1"))
    assert r.status_code == 200
    r = await client.post(
        "/api/events",
        json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "batch_failed",
            "timestamp": "2026-04-26T08:10:00Z",
            "batch_id": "fail-1",
            "source": {"project": "proj"},
            "data": {"n_done": 0, "n_failed": 1, "error": "OOM"},
        },
    )
    assert r.status_code == 200

    r = await client.post("/api/batches/fail-1/rerun", json={"overrides": {}})
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# AgentCommand enqueue + poll round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_enqueues_agent_command_when_host_has_agent(client):
    """When an agent is registered for the source host, request_rerun
    queues a ``rerun`` ``AgentCommand`` on it. The agent's poll endpoint
    surfaces it with the source's command + cwd in the payload."""
    await _seed_terminal_batch(client, "queue-1", host="host-X")
    agent_id, agent_token = await _register_agent(client, "host-X")

    r = await client.post(
        "/api/batches/queue-1/rerun",
        json={"overrides": {"model.d_model": 256}},
    )
    assert r.status_code == 201, r.text
    new_batch_id = r.json()["batch_id"]

    # Agent polls — the new rerun command must appear.
    r = await client.get(
        f"/api/agents/{agent_id}/jobs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200, r.text
    jobs = r.json()["jobs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["kind"] == "rerun"
    assert job["batch_id"] == new_batch_id
    assert job["payload"]["source_batch_id"] == "queue-1"
    assert job["payload"]["command"] == "main.py experiment=spike_v5"
    assert job["payload"]["overrides"] == {"model.d_model": 256}


@pytest.mark.asyncio
async def test_rerun_no_agent_still_creates_batch(client):
    """If no agent is registered for the source host, the rerun still
    succeeds (Batch row + event) but no AgentCommand is enqueued. The
    operator can either start an agent later or run the command by hand
    — this is the design doc's "documented escape hatch" (Section 2).
    """
    await _seed_terminal_batch(client, "no-agent-1", host="bare-metal-host")
    # Note: NO _register_agent call.

    r = await client.post(
        "/api/batches/no-agent-1/rerun", json={"overrides": {}}
    )
    assert r.status_code == 201, r.text
    new_id = r.json()["batch_id"]

    # The new batch exists in 'requested' but no command landed in the
    # agent's queue (we register one *after* the rerun to inspect).
    other_agent_id, other_agent_token = await _register_agent(
        client, "different-host"
    )
    r = await client.get(
        f"/api/agents/{other_agent_id}/jobs",
        headers={"Authorization": f"Bearer {other_agent_token}"},
    )
    assert r.status_code == 200
    assert r.json()["jobs"] == []

    # And the new batch is fetchable + still 'requested'.
    r = await client.get(f"/api/batches/{new_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "requested"


# ---------------------------------------------------------------------------
# Idempotency dedupe — keyed on (source_batch_id, kind, status='pending')
# with NO time window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_idempotent_within_window(client):
    """Re-clicking Rerun on the same source collapses to one AgentCommand.

    Both responses point at the *same* new_batch_id so the UI doesn't
    fragment its routing. The dedupe key is
    ``(source_batch_id, kind='rerun', status='pending')`` — no time
    window means even a slow second click (well past the old 60 s
    cutoff) still hits the dedupe path so long as the agent hasn't
    acked yet."""
    await _seed_terminal_batch(client, "dedup-1", host="host-D")
    agent_id, agent_token = await _register_agent(client, "host-D")

    r1 = await client.post("/api/batches/dedup-1/rerun", json={"overrides": {}})
    assert r1.status_code == 201, r1.text

    r2 = await client.post("/api/batches/dedup-1/rerun", json={"overrides": {}})
    assert r2.status_code == 201, r2.text

    # Same new batch id (deduped, not a fresh clone).
    assert r1.json()["batch_id"] == r2.json()["batch_id"]

    # Only one pending agent command despite two clicks.
    r = await client.get(
        f"/api/agents/{agent_id}/jobs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    assert len(r.json()["jobs"]) == 1


@pytest.mark.asyncio
async def test_rerun_idempotent_when_agent_offline_for_long_period(client):
    """Even if 60s+ passes without agent ack, second rerun must reuse first.

    Regression guard for the race fixed in #103 review B2: the earlier
    implementation bounded dedupe to ``created_at >= now - 60s``, so a
    second click after the agent had been offline for >60 s would mint
    a duplicate Batch + AgentCommand. The fix dropped the time window,
    making the lookup purely ``(source_batch_id, kind, status='pending')``.

    This test rewrites the first command's ``created_at`` to a past
    timestamp (cheaper than ``time.sleep`` and deterministic) and
    verifies the second click still dedupes.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select, update

    from backend.db import SessionLocal
    from backend.models import AgentCommand

    await _seed_terminal_batch(client, "dedup-aged-1", host="host-OFF")
    agent_id, agent_token = await _register_agent(client, "host-OFF")

    # First click — mints batch + AgentCommand.
    r1 = await client.post(
        "/api/batches/dedup-aged-1/rerun", json={"overrides": {}}
    )
    assert r1.status_code == 201, r1.text

    # Backdate the AgentCommand by 10 minutes — well past the old 60 s
    # cutoff. If the time window were still in place this would force
    # the next click into the "mint a fresh row" branch.
    aged_iso = (
        (datetime.now(timezone.utc) - timedelta(minutes=10))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    async with SessionLocal() as s:
        await s.execute(
            update(AgentCommand)
            .where(AgentCommand.kind == "rerun")
            .where(AgentCommand.status == "pending")
            .values(created_at=aged_iso)
        )
        await s.commit()

    # Second click — must reuse the first row, not mint a duplicate.
    r2 = await client.post(
        "/api/batches/dedup-aged-1/rerun", json={"overrides": {}}
    )
    assert r2.status_code == 201, r2.text
    assert r1.json()["batch_id"] == r2.json()["batch_id"]

    # And the agent's queue still has exactly one pending command.
    r = await client.get(
        f"/api/agents/{agent_id}/jobs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    assert len(r.json()["jobs"]) == 1


@pytest.mark.asyncio
async def test_stop_cancels_pending_rerun_command(client):
    """If user stops a still-requested rerun, the agent must NOT spawn it.

    Without this fix the rerun's AgentCommand stays ``pending`` after a
    stop, so the next agent poll would happily ack it and start a
    subprocess against the now-stopped batch. The fix flips matching
    pending rerun commands to ``status='cancelled'`` inside
    :meth:`Executor.request_stop`.
    """
    from sqlalchemy import select

    from backend.db import SessionLocal
    from backend.models import AgentCommand

    await _seed_terminal_batch(client, "stop-cancel-src", host="host-SC")
    agent_id, agent_token = await _register_agent(client, "host-SC")

    # Mint the rerun (spawns child Batch + pending AgentCommand).
    r = await client.post(
        "/api/batches/stop-cancel-src/rerun", json={"overrides": {}}
    )
    assert r.status_code == 201, r.text
    new_batch_id = r.json()["batch_id"]

    # Stop the freshly-minted rerun before the agent acks.
    r = await client.post(f"/api/batches/{new_batch_id}/stop")
    assert r.status_code == 200, r.text

    # The pending rerun command must now be cancelled.
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(AgentCommand)
                .where(AgentCommand.batch_id == new_batch_id)
                .where(AgentCommand.kind == "rerun")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "cancelled"

    # Agent poll should NOT see the cancelled rerun (only the new
    # ``kind='stop'`` command queued by request_stop).
    r = await client.get(
        f"/api/agents/{agent_id}/jobs",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    kinds = [j["kind"] for j in jobs]
    assert "rerun" not in kinds
    assert "stop" in kinds


# ---------------------------------------------------------------------------
# Stop integration — refactored route still emits the legacy event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_route_still_writes_legacy_event(client):
    """``POST /batches/{id}/stop`` was refactored to delegate to the
    Executor. Old reporters that poll ``/stop-requested`` must still
    see the same event row + batch status flip — this regression
    guard stops a future Executor change from breaking that contract.
    """
    r = await client.post("/api/events", json=_batch_start_event("stop-me"))
    assert r.status_code == 200

    r = await client.post("/api/batches/stop-me/stop")
    assert r.status_code == 200
    assert r.json() == {"status": "stopping", "batch_id": "stop-me"}

    # Poll endpoint sees the request.
    r = await client.get("/api/batches/stop-me/stop-requested")
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] is True
    assert body["requested_by"] == "tester"


@pytest.mark.asyncio
async def test_stop_idempotent(client):
    """Re-clicking Stop on an already-stopping batch is a 200 no-op
    (the legacy contract, preserved by ``StopResult.noop``)."""
    r = await client.post("/api/events", json=_batch_start_event("stop-x"))
    assert r.status_code == 200
    r = await client.post("/api/batches/stop-x/stop")
    assert r.status_code == 200

    # Second call — same response, no double-event.
    r = await client.post("/api/batches/stop-x/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "stopping"
