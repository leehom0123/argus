"""SSE live event stream — ``GET /api/events/stream``.

Browser-side clients use ``new EventSource('/api/events/stream?...')`` to
receive real-time updates from the ingest pipeline without polling.

Authentication
--------------
Native :class:`EventSource` cannot set an ``Authorization`` header, so
this endpoint accepts the bearer token via **either**:

* ``Authorization: Bearer <token>`` header (preferred; used by
  server-to-server fetch callers and the reporter CLI), OR
* ``?token=<bearer>`` query parameter (required for
  browser-native ``EventSource``).

Both JWTs and ``em_live_`` / ``em_view_`` API tokens are accepted — the
connection is read-only, so scope rules are lighter than the ingest
path. Visibility is enforced on ``batch_id`` subscriptions via the
standard :class:`VisibilityResolver`.

Filter semantics
----------------
Query params compose an AND filter:

* ``batch_id=<id>`` — stream only events for this batch (visibility-
  checked against the caller). Most common for the live-batch page.
* ``project=<name>`` — stream all events in a project.
* ``host=<host>`` — stream events reported from a specific host.
* *(none)* — firehose; admin-only to avoid data leaks.

When any non-batch-id filter is present and the user isn't an admin,
we 403 rather than silently leak cross-user events (MVP keeps the
filter simple; Phase 2 can scope project filters to the user's
projects).

Design notes
------------
* Keepalive every 15s when no real event arrives, so intermediaries
  (nginx, cloudflare) don't idle-time the connection.
* Cleanup is in ``finally`` of the generator — when the browser closes
  the tab, ``StreamingResponse`` cancels the task and we unsubscribe.
* Visibility is checked once at connect. If a share is revoked
  mid-stream, the client keeps receiving until it reconnects. Phase-2
  TODO: periodic re-check.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.jwt import JWTError, decode_token, is_blacklisted
from backend.auth.tokens import lookup_token
from backend.db import SessionLocal
from backend.deps_locale import SupportedLocale, get_locale
from backend.i18n import tr
from backend.models import Job, User
from backend.services.sse_hub import hub
from backend.services.visibility import VisibilityResolver
from backend.utils.sse import format_keepalive, format_sse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["events"])


# Seconds between synthetic ``keepalive`` frames when the upstream is
# quiet. 15s is well under the common 30s load-balancer idle timeout.
KEEPALIVE_INTERVAL_S: float = 15.0


async def _resolve_user_from_token_string(
    token: str, db: AsyncSession
) -> Optional[User]:
    """Decode either a JWT or an API token to a :class:`User`.

    Returns ``None`` on any auth failure so the caller can emit a
    consistent 401 without leaking which branch failed.
    """
    if not token:
        return None

    # API token branch (``em_live_*`` / ``em_view_*``).
    if token.startswith("em_live_") or token.startswith("em_view_"):
        row = await lookup_token(db, token)
        if row is None or row.user is None or not row.user.is_active:
            return None
        return row.user

    # JWT branch.
    try:
        payload = decode_token(token)
    except JWTError:
        return None
    if await is_blacklisted(token):
        return None
    user = await db.get(User, int(payload["user_id"]))
    if user is None or not user.is_active:
        return None
    return user


async def _authenticate_stream(
    request: Request,
    token_query: Optional[str],
    db: AsyncSession,
    locale: SupportedLocale = "en-US",
) -> User:
    """Pick a token out of the query or header and resolve the user.

    Query-param tokens are permitted here (unlike the rest of the API)
    because native ``EventSource`` cannot attach custom headers. We
    still prefer the header if both are present — it's the safer of
    the two and keeps the token out of access logs in server-side use.
    """
    token: Optional[str] = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip() or None
    if not token and token_query:
        token = token_query.strip() or None
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=tr(locale, "sse.auth.required"),
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await _resolve_user_from_token_string(token, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=tr(locale, "sse.auth.invalid"),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def _enforce_subscribe_visibility(
    user: User,
    batch_id: Optional[str],
    project: Optional[str],
    host: Optional[str],
    db: AsyncSession,
    locale: SupportedLocale = "en-US",
) -> None:
    """Authorize the requested filter against the caller's view.

    Rules:
      * ``batch_id`` provided — must pass ``can_view_batch``. This is
        the only filter flavour non-admins can use today.
      * Any other filter (``project`` / ``host`` / none) — admin-only.
        Phase 2 can narrow project filters to projects the user owns
        or has shares in.
    """
    if batch_id:
        resolver = VisibilityResolver()
        if not await resolver.can_view_batch(user, batch_id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=tr(locale, "sse.batch.no_access"),
            )
        return

    # No batch_id → broader subscription. Keep it admin-only for MVP.
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=tr(locale, "sse.admin_required"),
        )


async def _sse_generator(
    request: Request,
    queue: asyncio.Queue,
    sid: int,
) -> AsyncIterator[str]:
    """Yield SSE frames from ``queue`` with a keepalive heartbeat.

    Design notes:
      * The outer loop polls ``request.is_disconnected()`` on every
        iteration so a closed browser tab doesn't leave the generator
        hanging on :meth:`asyncio.Queue.get` for the full keepalive
        window.
      * The wait is chunked into sub-intervals so disconnect detection
        latency stays bounded (otherwise we'd wait up to
        ``KEEPALIVE_INTERVAL_S`` before noticing the client left).
      * ``finally`` always unsubscribes so a torn connection doesn't
        leak a queue into the hub table.
    """
    try:
        # Initial hello — helps clients verify the pipe before the
        # first real event. Also unblocks test harnesses that want to
        # confirm the subscription landed.
        yield format_sse("hello", {"subscribed": True, "sid": sid})

        # Sub-interval the keepalive so disconnect detection happens
        # quickly in tests / on proxies that buffer rather than reset
        # the underlying socket. 0.5s is short enough for interactive
        # tests and long enough not to burn CPU in production.
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

            elapsed = 0.0  # any real event resets the keepalive clock
            event_type = event.get("event_type") or "message"
            yield format_sse(event_type, event)
    except asyncio.CancelledError:
        # StreamingResponse raises this on client disconnect; don't
        # swallow — the ``finally`` block still runs.
        log.debug("sse generator sid=%d cancelled", sid)
        raise
    finally:
        await hub.unsubscribe(sid)


@router.get(
    "/events/stream",
    deprecated=True,
    description=(
        "Deprecated since v0.2 — use ``GET /api/sse?channels=batch:<id>`` "
        "instead. Will be removed in v0.3."
    ),
)
async def stream_events(
    request: Request,
    batch_id: Optional[str] = Query(default=None),
    project: Optional[str] = Query(default=None),
    host: Optional[str] = Query(default=None),
    token: Optional[str] = Query(
        default=None,
        description=(
            "Bearer token for browser EventSource clients that cannot "
            "set the Authorization header."
        ),
    ),
    locale: SupportedLocale = Depends(get_locale),
) -> StreamingResponse:
    """Server-Sent Events stream scoped by the query filter.

    Returns a ``text/event-stream`` long-poll. Each event is emitted as
    ``event: <event_type>\\ndata: <json>\\n\\n``. ``keepalive`` frames
    arrive every 15s when the pipe is quiet. ``hello`` is sent on
    connect.

    We deliberately do NOT use the standard ``Depends(get_session)``:
    FastAPI keeps dependency-injected sessions open for the life of
    the response, which for SSE could be hours. Instead we open a
    short-lived session just for the auth + visibility check and close
    it before the generator starts emitting — the stream itself only
    talks to the hub.
    """
    async with SessionLocal() as db:
        user = await _authenticate_stream(request, token, db, locale)
        await _enforce_subscribe_visibility(
            user, batch_id, project, host, db, locale
        )

    filt: dict = {}
    if batch_id:
        filt["batch_id"] = batch_id
    if project:
        filt["project"] = project
    if host:
        filt["host"] = host

    sid, queue = await hub.subscribe(filt)
    log.info(
        "sse connected user=%s sid=%d filter=%r", user.username, sid, filt
    )

    # ``media_type`` pins the right Content-Type. Disabling the
    # caching headers defends against middleboxes rewriting the stream.
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",  # nginx: don't buffer SSE frames.
    }
    return StreamingResponse(
        _sse_generator(request, queue, sid),
        media_type="text/event-stream",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Live log-tail streams — PM roadmap #4
# ---------------------------------------------------------------------------
#
# These endpoints share the hub + auth machinery above but restrict the
# emitted frames to ``log_line`` events. The only non-trivial piece is
# the "one concurrent stream per user+batch" rule: opening a second tail
# closes the first — this matches how people actually use "tail -f"
# (you don't want two tabs racing over the same pipe).
#
# The drop-oldest registry is a plain dict of ``(user_id, batch_id) →
# asyncio.Event``. When a new stream opens we set the previous owner's
# event; the SSE generator polls the event alongside ``is_disconnected``
# and returns cleanly, which unsubscribes it from the hub. In-process
# state only — acceptable for the same reason :mod:`sse_hub` is
# per-process.


# Key: ``(user_id, batch_id)`` — value: :class:`asyncio.Event`. Setting
# the event asks the owning generator to close its iteration.
_log_stream_displacement: dict[tuple[int, str], asyncio.Event] = {}
_log_stream_lock: asyncio.Lock = asyncio.Lock()


async def _claim_log_stream_slot(
    user_id: int, batch_id: str
) -> asyncio.Event:
    """Register this connection as the sole owner for ``(user, batch)``.

    Returns a fresh :class:`asyncio.Event` that the current stream owns
    and should poll; if a later request steals the slot, the event will
    be set externally and the generator exits cleanly.
    """
    key = (user_id, batch_id)
    evict = asyncio.Event()
    async with _log_stream_lock:
        previous = _log_stream_displacement.get(key)
        _log_stream_displacement[key] = evict
    if previous is not None:
        # Tell the previous owner to bail. They'll unsubscribe in their
        # own ``finally`` block.
        previous.set()
    return evict


async def _release_log_stream_slot(
    user_id: int, batch_id: str, evict: asyncio.Event
) -> None:
    """Drop our claim on the slot, only if we still hold it.

    We compare against the stored event so a raced ``_claim`` call from a
    newer connection doesn't lose its own registration on our cleanup.
    """
    key = (user_id, batch_id)
    async with _log_stream_lock:
        current = _log_stream_displacement.get(key)
        if current is evict:
            _log_stream_displacement.pop(key, None)


async def _log_sse_generator(
    request: Request,
    queue: asyncio.Queue,
    sid: int,
    user_id: int,
    batch_id: str,
    evict: asyncio.Event,
    job_id: Optional[str] = None,
) -> AsyncIterator[str]:
    """Variant of :func:`_sse_generator` that only yields ``log_line`` frames.

    Adds two behaviours on top of the base generator:

    * ``job_id`` post-filter — the hub filter key set is
      ``(batch_id, project, host)``, so per-job filtering happens here
      in user-space. This is fine: a single batch rarely produces more
      log_line traffic than one consumer can keep up with.
    * ``evict`` handling — a concurrent claim on ``(user, batch)`` sets
      the event; we treat it like a disconnect and exit cleanly.
    """
    try:
        yield format_sse(
            "hello",
            {"subscribed": True, "sid": sid, "stream": "log_tail"},
        )

        poll_step = 0.5
        elapsed = 0.0
        while True:
            if await request.is_disconnected():
                break
            if evict.is_set():
                # Surface a reason so the client can show "displaced by
                # another tab" rather than a blind disconnect.
                yield format_sse(
                    "displaced",
                    {"reason": "another stream took over this batch"},
                )
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
            if event.get("event_type") != "log_line":
                # Hub doesn't filter by event_type; skip non-log frames
                # so tail consumers aren't flooded by epochs / resources.
                continue
            if job_id is not None and event.get("job_id") != job_id:
                continue
            yield format_sse("log_line", event)
    except asyncio.CancelledError:
        log.debug("log sse generator sid=%d cancelled", sid)
        raise
    finally:
        await hub.unsubscribe(sid)
        await _release_log_stream_slot(user_id, batch_id, evict)


def _log_stream_response_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }


@router.get(
    "/batches/{batch_id}/logs/stream",
    deprecated=True,
    description=(
        "Deprecated since v0.2 — fold the batch's log_line frames into "
        "``GET /api/sse?channels=batch:<id>`` and filter client-side. "
        "Will be removed in v0.3."
    ),
)
async def stream_batch_logs(
    batch_id: str,
    request: Request,
    token: Optional[str] = Query(
        default=None,
        description=(
            "Bearer token for browser EventSource clients that cannot "
            "set the Authorization header."
        ),
    ),
    locale: SupportedLocale = Depends(get_locale),
) -> StreamingResponse:
    """SSE live tail of ``log_line`` events for one batch.

    Auth + visibility mirror :func:`stream_events`. Concurrency is capped
    at 1 active stream per ``(user, batch_id)`` — opening a new one
    displaces the previous with a ``displaced`` SSE event so the older
    client can show a clean "taken over" banner.
    """
    async with SessionLocal() as db:
        user = await _authenticate_stream(request, token, db, locale)
        await _enforce_subscribe_visibility(
            user, batch_id, None, None, db, locale
        )

    evict = await _claim_log_stream_slot(user.id, batch_id)
    sid, queue = await hub.subscribe({"batch_id": batch_id})
    log.info(
        "sse log-tail connected user=%s sid=%d batch_id=%s",
        user.username, sid, batch_id,
    )

    return StreamingResponse(
        _log_sse_generator(request, queue, sid, user.id, batch_id, evict),
        media_type="text/event-stream",
        headers=_log_stream_response_headers(),
    )


@router.get(
    "/jobs/{batch_id}/{job_id}/logs/stream",
    deprecated=True,
    description=(
        "Deprecated since v0.2 — use "
        "``GET /api/sse?channels=job:<batch>:<job>`` and filter to "
        "log_line client-side. Will be removed in v0.3."
    ),
)
async def stream_job_logs(
    batch_id: str,
    job_id: str,
    request: Request,
    token: Optional[str] = Query(default=None),
    locale: SupportedLocale = Depends(get_locale),
) -> StreamingResponse:
    """SSE live tail filtered to a single job within a batch.

    Matches the ``GET /api/jobs/{batch_id}/{job_id}`` address shape used
    by :mod:`backend.api.jobs` — job ids are only unique within a batch,
    so we need the composite path rather than a bare ``{id}``.

    The rate-limit slot is still keyed on ``(user, batch_id)`` per the
    PM spec (one tail per user per batch). Job-level filtering happens
    in the generator after the visibility check.
    """
    async with SessionLocal() as db:
        user = await _authenticate_stream(request, token, db, locale)
        await _enforce_subscribe_visibility(
            user, batch_id, None, None, db, locale
        )
        # Validate the job actually belongs to this batch — avoids
        # subscribing to a doomed filter that will never match, and
        # gives the client a crisp 404 up-front.
        job = await db.get(Job, (job_id, batch_id))
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=tr(locale, "job.not_found"),
            )

    evict = await _claim_log_stream_slot(user.id, batch_id)
    sid, queue = await hub.subscribe({"batch_id": batch_id})
    log.info(
        "sse log-tail connected user=%s sid=%d batch_id=%s job_id=%s",
        user.username, sid, batch_id, job_id,
    )

    return StreamingResponse(
        _log_sse_generator(
            request, queue, sid, user.id, batch_id, evict, job_id=job_id
        ),
        media_type="text/event-stream",
        headers=_log_stream_response_headers(),
    )
