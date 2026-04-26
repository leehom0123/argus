"""Basic SSE subscribe / publish / disconnect semantics.

These tests talk directly to :class:`SSEHub` because driving a full
``StreamingResponse`` through ``ASGITransport`` and re-reading chunks in
the same event-loop races with the response-generation task. The API
surface is covered by :mod:`test_sse_auth` and :mod:`test_sse_keepalive`
where we only need to assert status codes + initial frames.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.services.sse_hub import SSEHub


# ---------------------------------------------------------------------------
# Direct hub behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hub_publish_routes_to_matching_batch_id_only():
    """A subscriber filtering on batch_id only sees matching events."""
    hub = SSEHub()
    sid_a, queue_a = await hub.subscribe({"batch_id": "batch-A"})
    sid_b, queue_b = await hub.subscribe({"batch_id": "batch-B"})

    await hub.publish({
        "event_type": "job_epoch",
        "batch_id": "batch-A",
        "source": {"project": "p"},
        "data": {"epoch": 1},
    })

    # A receives; B does not.
    ev = await asyncio.wait_for(queue_a.get(), timeout=0.5)
    assert ev["batch_id"] == "batch-A"
    assert queue_b.empty()

    await hub.unsubscribe(sid_a)
    await hub.unsubscribe(sid_b)


@pytest.mark.asyncio
async def test_hub_empty_filter_subscribes_to_everything():
    """Empty dict filter = firehose."""
    hub = SSEHub()
    sid, queue = await hub.subscribe({})

    await hub.publish({"event_type": "x", "batch_id": "anything",
                       "source": {"project": "p"}})
    await hub.publish({"event_type": "y", "batch_id": "other",
                       "source": {"project": "q", "host": "h"}})

    ev1 = await asyncio.wait_for(queue.get(), timeout=0.5)
    ev2 = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert ev1["event_type"] == "x"
    assert ev2["event_type"] == "y"
    await hub.unsubscribe(sid)


@pytest.mark.asyncio
async def test_hub_project_and_host_filters_combine_and():
    """Multiple filter keys form a conjunction."""
    hub = SSEHub()
    sid, queue = await hub.subscribe({"project": "p1", "host": "h1"})

    # Only last event matches both.
    await hub.publish({"event_type": "a", "batch_id": "b",
                       "source": {"project": "p2", "host": "h1"}})
    await hub.publish({"event_type": "b", "batch_id": "b",
                       "source": {"project": "p1", "host": "h2"}})
    await hub.publish({"event_type": "c", "batch_id": "b",
                       "source": {"project": "p1", "host": "h1"}})

    ev = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert ev["event_type"] == "c"
    assert queue.empty()
    await hub.unsubscribe(sid)


@pytest.mark.asyncio
async def test_hub_unsubscribe_removes_subscription():
    """After unsubscribe the sid is no longer delivered to."""
    hub = SSEHub()
    sid, queue = await hub.subscribe({"batch_id": "b1"})
    assert hub._subscription_count() == 1

    await hub.unsubscribe(sid)
    assert hub._subscription_count() == 0

    # Publishing now doesn't raise and doesn't touch the (now-detached) queue.
    await hub.publish({"event_type": "x", "batch_id": "b1",
                       "source": {"project": "p"}})
    assert queue.empty()

    # Unsubscribe is idempotent.
    await hub.unsubscribe(sid)


@pytest.mark.asyncio
async def test_hub_unsubscribe_unknown_sid_is_noop():
    hub = SSEHub()
    await hub.unsubscribe(9999)  # must not raise
    assert hub._subscription_count() == 0


# ---------------------------------------------------------------------------
# Integration: POST event → hub.publish ran in the request handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_event_publishes_to_hub(client):
    """A POST /api/events commit triggers an async hub.publish."""
    from backend.services import sse_hub as hub_mod

    # Swap in a fresh hub so the default singleton's state (from other
    # tests running concurrently) doesn't pollute this one.
    hub = hub_mod.SSEHub()
    original = hub_mod.hub
    hub_mod.hub = hub
    try:
        batch_id = "sse-pub-" + uuid.uuid4().hex[:8]
        sid, queue = await hub.subscribe({"batch_id": batch_id})

        event = {
            "schema_version": "1.1",
            "event_id": str(uuid.uuid4()),
            "event_type": "job_epoch",
            "timestamp": "2026-04-23T09:00:00Z",
            "batch_id": batch_id,
            "job_id": "j1",
            "source": {"project": "p"},
            "data": {"epoch": 1, "train_loss": 0.5},
        }
        r = await client.post("/api/events", json=event)
        assert r.status_code == 200, r.text

        # publish_to_sse is scheduled on the loop; it should arrive promptly.
        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received["batch_id"] == batch_id
        assert received["event_type"] == "job_epoch"
        assert received["data"]["epoch"] == 1
        await hub.unsubscribe(sid)
    finally:
        hub_mod.hub = original


@pytest.mark.asyncio
async def test_batch_post_publishes_per_event(client):
    """Each committed event in a batch publishes once."""
    from backend.services import sse_hub as hub_mod

    hub = hub_mod.SSEHub()
    original = hub_mod.hub
    hub_mod.hub = hub
    try:
        batch_id = "sse-bulk-" + uuid.uuid4().hex[:8]
        sid, queue = await hub.subscribe({"batch_id": batch_id})

        events = [
            {
                "schema_version": "1.1",
                "event_id": str(uuid.uuid4()),
                "event_type": "job_epoch",
                "timestamp": "2026-04-23T09:00:00Z",
                "batch_id": batch_id,
                "job_id": f"j{i}",
                "source": {"project": "p"},
                "data": {"epoch": 1, "train_loss": 0.5},
            }
            for i in range(3)
        ]
        r = await client.post("/api/events/batch", json={"events": events})
        assert r.status_code == 200
        assert r.json()["accepted"] == 3

        received = []
        for _ in range(3):
            ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            received.append(ev)
        assert len(received) == 3
        assert {e["job_id"] for e in received} == {"j0", "j1", "j2"}
        await hub.unsubscribe(sid)
    finally:
        hub_mod.hub = original


@pytest.mark.asyncio
async def test_disconnect_unsubscribes_from_hub(client):
    """When the HTTP client disconnects, the hub drops the subscription."""
    from backend.services import sse_hub as hub_mod
    from backend.tests.test_sse_auth import _drive_sse_request

    hub = hub_mod.SSEHub()
    original = hub_mod.hub
    hub_mod.hub = hub
    try:
        assert hub._subscription_count() == 0
        app = client._transport.app  # type: ignore[attr-defined]
        token = getattr(client, "_test_default_token")

        status_code, _, chunk = await _drive_sse_request(
            app, f"/api/events/stream?token={token}"
        )
        assert status_code == 200
        assert chunk.startswith(b"event: hello")

        # Generator's finally block runs on disconnect; by the time
        # _drive_sse_request returned, that's already done. Allow one
        # loop iteration for any pending callbacks to settle.
        await asyncio.sleep(0.05)
        assert hub._subscription_count() == 0, (
            "hub should have no live subscriptions after disconnect; "
            f"state: {hub._subs!r}"
        )
    finally:
        hub_mod.hub = original


@pytest.mark.asyncio
async def test_dedup_event_does_not_publish(client):
    """Replaying an event_id should not re-fire SSE."""
    from backend.services import sse_hub as hub_mod

    hub = hub_mod.SSEHub()
    original = hub_mod.hub
    hub_mod.hub = hub
    try:
        batch_id = "sse-dedup-" + uuid.uuid4().hex[:8]
        event_id = str(uuid.uuid4())
        sid, queue = await hub.subscribe({"batch_id": batch_id})

        event = {
            "schema_version": "1.1",
            "event_id": event_id,
            "event_type": "job_epoch",
            "timestamp": "2026-04-23T09:00:00Z",
            "batch_id": batch_id,
            "job_id": "j1",
            "source": {"project": "p"},
            "data": {"epoch": 1, "train_loss": 0.5},
        }
        r1 = await client.post("/api/events", json=event)
        assert r1.status_code == 200
        first = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert first["event_type"] == "job_epoch"

        # Replay — should dedupe and NOT publish.
        r2 = await client.post("/api/events", json=event)
        assert r2.status_code == 200
        assert r2.json()["deduplicated"] is True

        # Give the loop a tick to confirm nothing new arrived.
        await asyncio.sleep(0.1)
        assert queue.empty()
        await hub.unsubscribe(sid)
    finally:
        hub_mod.hub = original
