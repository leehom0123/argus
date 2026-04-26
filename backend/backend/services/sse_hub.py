"""In-memory Server-Sent Events pub/sub hub.

The hub is a thin fan-out between the ``/api/events`` ingest path and
the ``/api/events/stream`` subscribers. It is **per-process**:

* Subscribers register with a filter dict (``batch_id`` / ``project`` /
  ``host``; all keys must match; empty dict = subscribe-to-all).
* On :meth:`publish`, we iterate live subscriptions and non-blocking
  push into each matching queue. If a queue is full we drop the frame
  and emit a warning — this protects a slow or disconnected client
  from back-pressuring the ingest path.

TODO(phase-2): multi-process deploys need a shared bus (Redis pub/sub
or NATS). The publish interface stays the same; swap the in-memory
``_subs`` table for a Redis subscription manager per worker.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


# Match these top-level keys from an event dict. Kept small on purpose —
# SSE filters are a UX convenience, not an access-control boundary
# (visibility is enforced separately at subscribe time).
_FILTER_KEYS: tuple[str, ...] = ("batch_id", "project", "host")


class SSEHub:
    """Per-process SSE subscription manager.

    Each subscription owns one :class:`asyncio.Queue` with a fixed
    ``maxsize=100`` cap. Publish is non-blocking: on ``QueueFull`` the
    frame is dropped so one slow client doesn't stall ingest or starve
    the event loop.

    The hub is safe against concurrent subscribe / unsubscribe /
    publish calls (``_lock`` guards the subscription table).
    """

    # Bounded queue so a stalled reader can't balloon memory.
    QUEUE_MAXSIZE = 100

    def __init__(self) -> None:
        # sid → (queue, filter_dict). Kept as plain dict under lock;
        # we never iterate without snapshotting first so mutation during
        # publish is safe.
        self._subs: dict[int, tuple[asyncio.Queue, dict]] = {}
        self._next_id: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

    # ---- lifecycle --------------------------------------------------

    async def subscribe(self, filt: dict) -> tuple[int, asyncio.Queue]:
        """Register a new subscription.

        Parameters
        ----------
        filt:
            Dict of filter predicates. Supported keys:
            ``batch_id``, ``project``, ``host``. Missing/None values
            mean "don't filter on this key". Unknown keys are ignored
            (forward compatibility).

        Returns
        -------
        (sid, queue):
            ``sid`` is the opaque subscription id for
            :meth:`unsubscribe`. ``queue`` is the outbound event queue
            — drain it with ``await queue.get()``.
        """
        # Normalise: drop None and keys outside the allowed set so
        # _match stays trivial.
        clean: dict[str, Any] = {
            k: v for k, v in (filt or {}).items() if k in _FILTER_KEYS and v is not None
        }
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
        async with self._lock:
            sid = self._next_id
            self._next_id += 1
            self._subs[sid] = (queue, clean)
        log.debug("sse subscribe sid=%d filter=%r", sid, clean)
        return sid, queue

    async def unsubscribe(self, sid: int) -> None:
        """Drop a subscription. Idempotent — unknown ids are ignored."""
        async with self._lock:
            removed = self._subs.pop(sid, None)
        if removed is not None:
            log.debug("sse unsubscribe sid=%d", sid)

    # ---- fan-out ----------------------------------------------------

    async def publish(self, event: dict) -> None:
        """Push ``event`` to every matching subscriber.

        Non-blocking: each queue receives via ``put_nowait``. On
        :class:`asyncio.QueueFull` the frame is dropped and a warning
        is logged. We never ``await queue.put`` because one stuck
        reader would otherwise block ingest.

        Iteration is on a snapshot so subscribe/unsubscribe during a
        publish don't raise ``RuntimeError: dictionary changed size
        during iteration``.
        """
        # Snapshot outside the lock is fine — subscribers that are
        # removed mid-publish simply get a stale frame put onto their
        # queue, which is then garbage-collected alongside the queue.
        async with self._lock:
            snapshot = list(self._subs.items())

        for sid, (queue, filt) in snapshot:
            if not self._match(event, filt):
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # One slow client shouldn't knock out the hub; log and
                # move on. Frontend can reconnect on EventSource close.
                log.warning(
                    "sse drop frame: sid=%d queue full (maxsize=%d) "
                    "event_type=%s batch_id=%s",
                    sid,
                    self.QUEUE_MAXSIZE,
                    event.get("event_type"),
                    event.get("batch_id"),
                )

    # ---- filter -----------------------------------------------------

    @staticmethod
    def _match(event: dict, filt: dict) -> bool:
        """AND-match ``event`` against ``filt``.

        * ``batch_id`` compared against ``event['batch_id']``
        * ``project`` / ``host`` compared against the nested
          ``event['source']['project']`` / ``['host']``
        * Empty filter always matches.
        """
        if not filt:
            return True

        if "batch_id" in filt and event.get("batch_id") != filt["batch_id"]:
            return False

        source = event.get("source") or {}
        if "project" in filt and source.get("project") != filt["project"]:
            return False
        if "host" in filt and source.get("host") != filt["host"]:
            return False

        return True

    # ---- test helpers -----------------------------------------------

    def _subscription_count(self) -> int:
        """Exposed for tests — size of the live subscription table."""
        return len(self._subs)


# Process-wide singleton. Importers should use this handle; we don't
# expose a factory because the hub is pure in-memory state.
hub: SSEHub = SSEHub()


def reset_hub_for_tests() -> None:
    """Reset the singleton's subscription table between tests."""
    global hub
    hub = SSEHub()


# ---------------------------------------------------------------------------
# Fire-and-forget publish wrapper
# ---------------------------------------------------------------------------


# Strong references to in-flight publish tasks. Without this the
# asyncio event loop only weak-refs the task and may GC it before it
# runs. See https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_pending_tasks: set[asyncio.Task] = set()


def publish_to_sse(event: dict) -> None:
    """Schedule ``hub.publish(event)`` on the running loop.

    Safe to call from inside a request handler after ``session.commit()``
    — returns immediately. Errors from the hub are logged, never raised
    back into the caller's response path.

    Keeps a strong reference to the task until it completes so the
    scheduler doesn't reap it early.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (e.g. called from a sync context) — best-effort log
        # and drop. Production request handlers are always inside a loop.
        log.debug("publish_to_sse called without a running loop; dropping")
        return

    task = loop.create_task(_safe_publish(event))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def _safe_publish(event: dict) -> None:
    """Invoke :meth:`SSEHub.publish` with exception shielding."""
    try:
        await hub.publish(event)
    except Exception as exc:  # noqa: BLE001
        log.warning("sse publish failed: %s", exc, exc_info=True)
