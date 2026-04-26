"""Multiplexed SSE endpoint — ``GET /api/sse?channels=...``.

Reduces frontend connection count by carrying multiple logical streams
on one HTTP/1.1 (or HTTP/2) connection. Replaces the N+2 connection
pattern (per-batch + per-job × N + dashboard) on pages like
:file:`BatchDetail.vue` with a single multiplexed stream.

Wire format
-----------
Each emitted frame carries an extra top-level ``channel`` field so the
client can demultiplex. The ``event:`` line and ``data:`` JSON are
otherwise identical to the single-channel endpoints. The initial
``hello`` carries the resolved channel set.

Channel selectors
-----------------
* ``batch:<batch_id>`` — same semantics as the legacy
  ``/api/events/stream?batch_id=X`` (visibility-checked).
* ``job:<batch_id>:<job_id>`` — events filtered to one job within a
  batch. Visibility is checked against the parent batch; job filtering
  is post-hub (the hub keys do not include ``job_id``).
* ``dashboard`` — admin-only firehose for counter / global updates.

Backward compatibility
----------------------
The single-channel endpoints (``/api/events/stream``,
``/api/batches/{id}/logs/stream``, ``/api/jobs/.../logs/stream``) stay
in place for v0.2. They are marked ``deprecated=True`` in OpenAPI;
removal is scheduled for v0.3.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from backend.api.events_stream import (
    _authenticate_stream,
    _enforce_subscribe_visibility,
)
from backend.db import SessionLocal
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Job
from backend.services.sse_hub import hub
from backend.utils.sse import format_keepalive, format_sse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["events"])


# Mirrors the keepalive cadence used by the legacy single-channel
# endpoint so middleboxes see consistent heartbeat behaviour regardless
# of which endpoint a client uses.
KEEPALIVE_INTERVAL_S: float = 15.0

# Defensive cap on the number of channels per multiplex connection —
# the broker subscribes once per channel, so an unbounded list would
# let one client tie up many hub slots. 64 covers BatchDetail's worst
# case (a batch with 60-ish visible jobs) with headroom.
MAX_CHANNELS_PER_CONNECTION: int = 64


# ---------------------------------------------------------------------------
# Channel parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Channel:
    """One logical sub-stream within a multiplexed connection.

    ``kind`` selects the matching strategy; the optional ``batch_id`` /
    ``job_id`` fill the matching predicate. The original selector
    string is kept as ``raw`` so we can echo it back to the client in
    the ``hello`` frame and tag every frame with the same string the
    caller asked for.
    """

    kind: str  # "batch" | "job" | "dashboard"
    raw: str
    batch_id: Optional[str] = None
    job_id: Optional[str] = None


def _parse_channels(channels_param: str) -> list[Channel]:
    """Split ``channels`` query into typed selectors.

    Empty / whitespace-only entries are skipped (so a trailing comma is
    tolerated). Unknown ``kind`` prefixes raise a 400 — silent ignore
    would mask client typos and produce silent drops.
    """
    out: list[Channel] = []
    seen: set[str] = set()
    for raw in channels_param.split(","):
        sel = raw.strip()
        if not sel:
            continue
        if sel in seen:
            # Subscribing twice to the same channel would double-deliver
            # frames; dedupe up-front so the broker stays simple.
            continue
        seen.add(sel)

        if sel == "dashboard":
            out.append(Channel(kind="dashboard", raw=sel))
            continue

        if sel.startswith("batch:"):
            bid = sel[len("batch:"):]
            if not bid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"channel selector missing batch_id: {sel!r}",
                )
            out.append(Channel(kind="batch", raw=sel, batch_id=bid))
            continue

        if sel.startswith("job:"):
            tail = sel[len("job:"):]
            # Accept ``job:<batch>:<job>`` (preferred — disambiguates
            # job ids that are only unique within a batch).
            if ":" not in tail:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"channel selector must be job:<batch>:<job>: {sel!r}"
                    ),
                )
            bid, _, jid = tail.partition(":")
            if not bid or not jid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"channel selector has empty parts: {sel!r}",
                )
            out.append(Channel(kind="job", raw=sel, batch_id=bid, job_id=jid))
            continue

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown channel kind: {sel!r}",
        )

    if not out:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channels query parameter must be non-empty",
        )
    if len(out) > MAX_CHANNELS_PER_CONNECTION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"too many channels ({len(out)}); cap is "
                f"{MAX_CHANNELS_PER_CONNECTION}"
            ),
        )
    return out


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


async def _enforce_channel_visibility(
    user, channels: Iterable[Channel], locale: SupportedLocale
) -> None:
    """Run the per-channel access check.

    Each channel maps onto one of the existing visibility predicates so
    the multiplex endpoint inherits the same rules as the legacy single-
    channel endpoints — a 403 here means the caller would be 403'd by
    the legacy endpoint too, with the same wording.
    """
    async with SessionLocal() as db:
        for ch in channels:
            if ch.kind == "batch":
                await _enforce_subscribe_visibility(
                    user, ch.batch_id, None, None, db, locale
                )
            elif ch.kind == "job":
                # job_id is post-hub filter; batch_id is the auth gate.
                await _enforce_subscribe_visibility(
                    user, ch.batch_id, None, None, db, locale
                )
                # Echo the upstream "validate parent batch owns the job"
                # check so a client can't smuggle traffic by faking a
                # job_id under an unrelated batch.
                job = await db.get(Job, (ch.job_id, ch.batch_id))
                if job is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=tr(locale, "job.not_found"),
                    )
            elif ch.kind == "dashboard":
                # Dashboard counters are derived from the firehose, so
                # gate the same way as the firehose subscription.
                if not user.is_admin:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=tr(locale, "sse.admin_required"),
                    )
            else:  # pragma: no cover — defensive
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"unknown channel kind: {ch.kind!r}",
                )


# ---------------------------------------------------------------------------
# Channel→event matching
# ---------------------------------------------------------------------------


def _channel_matches(ch: Channel, event: dict) -> bool:
    """Return True if ``event`` belongs to ``ch``.

    * ``batch`` — ``event.batch_id == ch.batch_id``.
    * ``job`` — ``event.batch_id == ch.batch_id`` AND
      ``event.job_id == ch.job_id``. Job-scoped events without a
      ``job_id`` (e.g. ``batch_done``) do not match a job channel.
    * ``dashboard`` — every event matches; the dashboard channel is a
      firehose. Admin gating already happened at subscribe time.
    """
    if ch.kind == "batch":
        return event.get("batch_id") == ch.batch_id
    if ch.kind == "job":
        return (
            event.get("batch_id") == ch.batch_id
            and event.get("job_id") == ch.job_id
        )
    if ch.kind == "dashboard":
        return True
    return False  # pragma: no cover — _parse_channels rejects unknowns


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


async def _multiplex_generator(
    request: Request,
    queue: asyncio.Queue,
    sids: list[int],
    channels: list[Channel],
) -> AsyncIterator[str]:
    """Yield SSE frames demultiplexable by ``channel`` field.

    Logic mirrors :func:`backend.api.events_stream._sse_generator`
    closely:

    * an opening ``hello`` frame echoes the resolved channel set so
      clients can verify the subscription matches what they asked for;
    * polling :meth:`Request.is_disconnected` between queue waits keeps
      tear-down latency bounded;
    * keepalive every ``KEEPALIVE_INTERVAL_S`` so proxies don't kill
      idle pipes;
    * the ``finally`` block unsubscribes every sid we registered, even
      if some hub.subscribe calls were partial — leak-free shutdown is
      the whole point of doing this in one place.

    The fan-in pattern: every channel publishes onto **the same queue**
    (passed in by ``stream_multiplex``), so we don't need ``asyncio.
    wait`` over multiple queues here. We re-check the channel match in
    user-space because the hub's filter only knows about ``batch_id /
    project / host``; the dedicated job + dashboard semantics live
    here.
    """
    try:
        yield format_sse(
            "hello",
            {
                "subscribed": True,
                "channels": [ch.raw for ch in channels],
                "sids": sids,
            },
        )

        poll_step = 0.5
        elapsed = 0.0
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=poll_step)
            except asyncio.TimeoutError:
                elapsed += poll_step
                if elapsed >= KEEPALIVE_INTERVAL_S:
                    elapsed = 0.0
                    yield format_keepalive()
                continue

            elapsed = 0.0
            event_type = event.get("event_type") or "message"

            # An event may match multiple channels (e.g. an event for
            # batch B also matches job:B:J inside that batch). Emit one
            # frame per matching channel so the client demultiplexes
            # cleanly per channel without us guessing intent.
            for ch in channels:
                if not _channel_matches(ch, event):
                    continue
                payload = dict(event)
                payload["channel"] = ch.raw
                yield format_sse(event_type, payload)
    except asyncio.CancelledError:
        log.debug("multiplex generator cancelled (sids=%r)", sids)
        raise
    finally:
        # Unsubscribe everyone — order doesn't matter; idempotent on
        # unknown sids so a partial subscribe path is fine.
        for sid in sids:
            await hub.unsubscribe(sid)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/sse")
async def stream_multiplex(
    request: Request,
    channels: str = Query(
        ...,
        description=(
            "Comma-separated channel selectors. "
            "Allowed forms: 'batch:<id>', 'job:<batch>:<job>', 'dashboard'."
        ),
    ),
    token: Optional[str] = Query(
        default=None,
        description=(
            "Bearer token for browser EventSource clients that cannot "
            "set the Authorization header."
        ),
    ),
    locale: SupportedLocale = Depends(get_locale),
) -> StreamingResponse:
    """SSE multiplex stream — one HTTP connection, N logical channels.

    Each emitted frame carries a top-level ``channel`` field so the
    client can demultiplex. Frames are otherwise the same JSON payloads
    legacy single-channel endpoints emit; nothing about the schema
    changes.

    Auth + visibility are checked once at connect (per channel) — no
    mid-stream re-check, matching the legacy endpoint's behaviour.
    """
    parsed = _parse_channels(channels)

    async with SessionLocal() as db:
        user = await _authenticate_stream(request, token, db, locale)
    await _enforce_channel_visibility(user, parsed, locale)

    # One subscription per channel. We give every channel a hub-side
    # filter that narrows traffic as much as the hub can: batch + job
    # channels filter on ``batch_id``; dashboard subscribes to the
    # firehose because cross-batch events still need to reach it. The
    # generator's ``_channel_matches`` is the source of truth for
    # actual delivery, so a permissive hub filter is fine — it just
    # admits more candidates for in-process matching.
    fanin: asyncio.Queue = asyncio.Queue(maxsize=512)
    sids: list[int] = []

    async def _drain_into_fanin(src_queue: asyncio.Queue, source_sid: int):
        """Forward events from a per-subscription queue to the shared one.

        We can't simply share the queue across hub subscriptions: the
        hub creates a fresh queue per ``subscribe`` call and stores it
        in ``self._subs``. Forwarding is the cheapest way to merge
        without modifying the hub. ``put_nowait`` discards on overflow
        so one stalled client cannot back-pressure the hub.
        """
        try:
            while True:
                ev = await src_queue.get()
                try:
                    fanin.put_nowait(ev)
                except asyncio.QueueFull:
                    log.warning(
                        "multiplex fanin queue full, dropping frame "
                        "(sid=%d event_type=%s)",
                        source_sid,
                        ev.get("event_type"),
                    )
        except asyncio.CancelledError:
            # Normal teardown — the outer generator's finally cancels us.
            raise

    forwarder_tasks: list[asyncio.Task] = []
    try:
        for ch in parsed:
            if ch.kind == "dashboard":
                # Dashboard wants every event; admin-gated above.
                filt: dict = {}
            else:
                # Batch and job channels: narrow to the batch on the hub
                # side so unrelated traffic never even hits our queue.
                filt = {"batch_id": ch.batch_id}
            sid, q = await hub.subscribe(filt)
            sids.append(sid)
            forwarder_tasks.append(
                asyncio.create_task(_drain_into_fanin(q, sid))
            )
    except Exception:
        # Roll back any partial subscriptions + cancel any forwarders so
        # we don't leak hub state on a mid-setup failure.
        for sid in sids:
            await hub.unsubscribe(sid)
        for t in forwarder_tasks:
            t.cancel()
        raise

    log.info(
        "sse multiplex connected user=%s channels=%r sids=%r",
        user.username,
        [ch.raw for ch in parsed],
        sids,
    )

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }

    async def _gen() -> AsyncIterator[str]:
        # Wrap the generator so we can guarantee the forwarder tasks
        # are cancelled even if the generator raises before its own
        # ``finally`` has a chance to run.
        try:
            async for frame in _multiplex_generator(
                request, fanin, sids, parsed
            ):
                yield frame
        finally:
            for t in forwarder_tasks:
                t.cancel()
            # Best-effort wait so the cancellation actually propagates
            # before the response is closed; ignore errors here, the
            # tasks just need a tick to settle.
            for t in forwarder_tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers=headers,
    )
