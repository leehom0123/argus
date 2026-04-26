"""Integration tests for the SSE log-tail endpoints.

Covers ``GET /api/batches/{id}/logs/stream`` and
``GET /api/jobs/{batch_id}/{job_id}/logs/stream`` — the PM-roadmap-#4
"replace SSH-and-tail" feature. We drive the hub directly (as in
:mod:`test_sse_basic`) rather than through an HTTP stream reader so the
tests stay deterministic; the full HTTP handshake is already covered
by :mod:`test_sse_auth` / :mod:`test_sse_keepalive`.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.api import events_stream as es
from backend.services import sse_hub as hub_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bootstrap_batch(batch_id: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
    }


def _log_line_event(
    batch_id: str, job_id: str, line: str, level: str = "info"
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "log_line",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": "p"},
        "data": {"level": level, "line": line, "message": line},
    }


async def _collect_log_frames(
    batch_id: str,
    job_id: str | None,
    publish_events: list[dict],
    *,
    user_id: int = 1,
    timeout: float = 1.5,
) -> list[dict]:
    """Drive the log generator end-to-end without an ASGI layer.

    Swaps in a fresh :class:`SSEHub`, subscribes through the helpers in
    :mod:`events_stream`, publishes the supplied events, and yields the
    next few frames from the generator. The request mock only needs an
    ``is_disconnected`` coroutine — the generator uses nothing else.

    Note: ``events_stream.hub`` was ``from ... import hub``'d at module
    load, so we patch that module-level binding too — otherwise the
    generator's ``finally: await hub.unsubscribe(sid)`` would target
    the original process-wide singleton while the test's subscribe went
    to the fresh hub, leaking the subscription.
    """
    hub = hub_mod.SSEHub()
    original = hub_mod.hub
    original_stream_hub = es.hub
    hub_mod.hub = hub
    es.hub = hub

    class _StubRequest:
        """Minimal stand-in for :class:`fastapi.Request`."""

        async def is_disconnected(self) -> bool:
            return False

    try:
        evict = await es._claim_log_stream_slot(user_id, batch_id)
        sid, queue = await hub.subscribe({"batch_id": batch_id})
        gen = es._log_sse_generator(
            _StubRequest(), queue, sid, user_id, batch_id, evict,
            job_id=job_id,
        )

        # Drain the ``hello`` frame so the publish-then-receive path is
        # predictable.
        hello = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
        assert hello.startswith("event: hello")

        for ev in publish_events:
            await hub.publish(ev)

        # Collect any frames that arrive within a short window. The
        # generator is infinite; we stop once we've seen the expected
        # log_line count or the stream goes idle.
        frames: list[str] = []
        expected_logs = sum(
            1
            for ev in publish_events
            if ev["event_type"] == "log_line"
            and (job_id is None or ev.get("job_id") == job_id)
        )
        seen_logs = 0
        while seen_logs < expected_logs:
            frame = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
            frames.append(frame)
            if frame.startswith("event: log_line"):
                seen_logs += 1

        # Close the generator so its ``finally`` (unsubscribe + release
        # slot) runs before the next test.
        await gen.aclose()
        return _parse_sse(frames)
    finally:
        hub_mod.hub = original
        es.hub = original_stream_hub
        # Ensure the slot is released even if aclose didn't run (e.g. on
        # an assertion failure above).
        async with es._log_stream_lock:
            es._log_stream_displacement.pop((user_id, batch_id), None)


def _parse_sse(frames: list[str]) -> list[dict]:
    """Return ``[{event, data}]`` tuples for ``log_line`` frames only."""
    import json

    out: list[dict] = []
    for frame in frames:
        lines = frame.strip().split("\n")
        event = next(
            (ln[len("event: "):] for ln in lines if ln.startswith("event: ")),
            None,
        )
        data_lines = [
            ln[len("data: "):] for ln in lines if ln.startswith("data: ")
        ]
        if event == "log_line" and data_lines:
            out.append({"event": event, "data": json.loads(
                "\n".join(data_lines)
            )})
    return out


# ---------------------------------------------------------------------------
# Core behaviour — subscribe, receive 3 log_line events, disconnect cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_log_stream_receives_three_events():
    """Three publishes → three log_line frames delivered in order."""
    batch_id = "log-b-" + uuid.uuid4().hex[:8]
    events = [
        _bootstrap_batch(batch_id),
        _log_line_event(batch_id, "j1", "line one"),
        _log_line_event(batch_id, "j1", "line two", level="warning"),
        # Non-log event should be filtered out by the generator.
        {
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_epoch",
            "timestamp": "2026-04-23T09:00:00Z",
            "batch_id": batch_id,
            "job_id": "j1",
            "source": {"project": "p"},
            "data": {"epoch": 1, "train_loss": 0.5},
        },
        _log_line_event(batch_id, "j1", "line three", level="error"),
    ]
    frames = await _collect_log_frames(batch_id, job_id=None,
                                       publish_events=events)
    lines = [f["data"]["data"]["line"] for f in frames]
    levels = [f["data"]["data"]["level"] for f in frames]
    assert lines == ["line one", "line two", "line three"]
    assert levels == ["info", "warning", "error"]


# ---------------------------------------------------------------------------
# Job-scoped stream drops traffic for other jobs in the same batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_stream_filters_by_job_id():
    """Same-batch events for other jobs don't surface on a job-scoped tail."""
    batch_id = "log-j-" + uuid.uuid4().hex[:8]
    events = [
        _log_line_event(batch_id, "j-a", "a1"),
        _log_line_event(batch_id, "j-b", "b1"),  # filtered out
        _log_line_event(batch_id, "j-a", "a2"),
        _log_line_event(batch_id, "j-b", "b2"),  # filtered out
        _log_line_event(batch_id, "j-a", "a3"),
    ]
    frames = await _collect_log_frames(batch_id, job_id="j-a",
                                       publish_events=events)
    assert [f["data"]["data"]["line"] for f in frames] == ["a1", "a2", "a3"]
    assert all(f["data"]["job_id"] == "j-a" for f in frames)


# ---------------------------------------------------------------------------
# Disconnect cleanup — no subscription leaks after the client drops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_stream_disconnect_unsubscribes_from_hub():
    """Closing the generator releases both the hub subscription and the slot."""
    batch_id = "log-d-" + uuid.uuid4().hex[:8]
    user_id = 42
    hub = hub_mod.SSEHub()
    original = hub_mod.hub
    original_stream_hub = es.hub
    hub_mod.hub = hub
    es.hub = hub

    class _Req:
        async def is_disconnected(self) -> bool:
            return False

    try:
        evict = await es._claim_log_stream_slot(user_id, batch_id)
        sid, queue = await hub.subscribe({"batch_id": batch_id})
        assert hub._subscription_count() == 1
        assert (user_id, batch_id) in es._log_stream_displacement

        gen = es._log_sse_generator(
            _Req(), queue, sid, user_id, batch_id, evict, job_id=None
        )
        hello = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert hello.startswith("event: hello")

        # aclose() runs the ``finally`` block — the same path an HTTP
        # disconnect walks when StreamingResponse is cancelled.
        await gen.aclose()

        # Hub and slot registry should both be empty again.
        await asyncio.sleep(0.05)
        assert hub._subscription_count() == 0
        assert (user_id, batch_id) not in es._log_stream_displacement
    finally:
        hub_mod.hub = original
        es.hub = original_stream_hub
        async with es._log_stream_lock:
            es._log_stream_displacement.pop((user_id, batch_id), None)


# ---------------------------------------------------------------------------
# Rate-limit: opening a second tail for the same (user, batch) evicts the first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_stream_for_same_user_batch_displaces_first():
    """Drop-oldest semantics — the previous tail exits with ``displaced``."""
    batch_id = "log-r-" + uuid.uuid4().hex[:8]
    user_id = 7
    hub = hub_mod.SSEHub()
    original = hub_mod.hub
    original_stream_hub = es.hub
    hub_mod.hub = hub
    es.hub = hub

    class _Req:
        async def is_disconnected(self) -> bool:
            return False

    try:
        # First claim + generator.
        evict_a = await es._claim_log_stream_slot(user_id, batch_id)
        sid_a, queue_a = await hub.subscribe({"batch_id": batch_id})
        gen_a = es._log_sse_generator(
            _Req(), queue_a, sid_a, user_id, batch_id, evict_a, job_id=None
        )
        hello_a = await asyncio.wait_for(gen_a.__anext__(), timeout=1.0)
        assert hello_a.startswith("event: hello")

        # Second claim for the same key should evict the first.
        evict_b = await es._claim_log_stream_slot(user_id, batch_id)
        assert evict_a.is_set(), "previous owner should have been signalled"
        assert not evict_b.is_set()

        # First generator, on its next iteration, emits ``displaced``
        # and then exits.
        displaced = await asyncio.wait_for(gen_a.__anext__(), timeout=1.0)
        assert displaced.startswith("event: displaced")
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen_a.__anext__(), timeout=1.0)

        # New owner is registered cleanly.
        assert es._log_stream_displacement[(user_id, batch_id)] is evict_b
    finally:
        hub_mod.hub = original
        es.hub = original_stream_hub
        async with es._log_stream_lock:
            es._log_stream_displacement.pop((user_id, batch_id), None)
