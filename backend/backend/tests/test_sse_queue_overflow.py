"""Queue-overflow safety: a stuck reader must not crash the hub.

If a subscriber isn't draining its queue, ``publish`` should drop the
frame (with a warning) rather than block or raise. The rest of the
fan-out must still work — other subscribers get their events.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from backend.services.sse_hub import SSEHub


@pytest.mark.asyncio
async def test_queue_overflow_drops_frames_without_crash(caplog):
    """Filling a subscriber's queue past ``QUEUE_MAXSIZE`` drops excess frames."""
    hub = SSEHub()
    assert hub.QUEUE_MAXSIZE == 100  # sanity: documented cap

    # Slow-reader subscriber: we never drain its queue.
    slow_sid, slow_q = await hub.subscribe({"batch_id": "b-overflow"})

    with caplog.at_level(logging.WARNING, logger="backend.services.sse_hub"):
        # Publish 150 events — queue fills at 100, the rest are dropped
        # with a logged warning rather than raising QueueFull up the
        # stack or blocking the publisher.
        for i in range(150):
            await hub.publish({
                "event_type": "job_epoch",
                "batch_id": "b-overflow",
                "source": {"project": "p"},
                "data": {"i": i},
            })

    # Queue capped at maxsize; no exception, no hang.
    assert slow_q.qsize() == hub.QUEUE_MAXSIZE

    # One drop warning per overflowed frame.
    drop_records = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "sse drop frame" in r.getMessage()
    ]
    assert len(drop_records) == 150 - hub.QUEUE_MAXSIZE, (
        f"expected {150 - hub.QUEUE_MAXSIZE} drop warnings, "
        f"got {len(drop_records)}"
    )

    # The 100 that DID make it in are the first 100 in publish order
    # (FIFO) — the overflow drops the tail.
    for expected_i in range(hub.QUEUE_MAXSIZE):
        ev = slow_q.get_nowait()
        assert ev["data"]["i"] == expected_i

    await hub.unsubscribe(slow_sid)


@pytest.mark.asyncio
async def test_overflow_on_one_subscriber_does_not_starve_others():
    """A full queue on sub A must not prevent delivery to sub B."""
    hub = SSEHub()

    slow_sid, slow_q = await hub.subscribe({"batch_id": "b-co"})
    fast_sid, fast_q = await hub.subscribe({"batch_id": "b-co"})

    # Fill the slow reader to capacity.
    for i in range(hub.QUEUE_MAXSIZE):
        await hub.publish({
            "event_type": "x",
            "batch_id": "b-co",
            "source": {"project": "p"},
            "data": {"i": i},
        })
    assert slow_q.full()

    # Drain fast so subsequent publishes fit; publish one more event.
    while not fast_q.empty():
        fast_q.get_nowait()

    await hub.publish({
        "event_type": "after_full",
        "batch_id": "b-co",
        "source": {"project": "p"},
        "data": {"i": 999},
    })

    # Fast reader sees the new event even though slow is maxed out.
    ev = await asyncio.wait_for(fast_q.get(), timeout=0.5)
    assert ev["event_type"] == "after_full"

    await hub.unsubscribe(slow_sid)
    await hub.unsubscribe(fast_sid)
