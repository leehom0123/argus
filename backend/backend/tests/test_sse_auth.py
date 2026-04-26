"""Auth + visibility gating on ``GET /api/events/stream``.

Covers:
  * no token → 401
  * bad token → 401
  * valid token + unauthorised batch_id → 403
  * valid token + own batch_id → 200 (connected stream)
  * non-admin firehose (no batch_id) → 403
  * admin firehose → 200

Why we use a direct ASGI call instead of ``client.stream(...)`` for the
happy-path tests: httpx's ``ASGITransport`` buffers the entire response
body before returning, which never completes for an infinite SSE
generator. The status / 4xx tests happily return from
``_authenticate_stream`` / ``_enforce_subscribe_visibility`` before
``StreamingResponse`` is created, so they go through the standard
client. For success cases we drive the ASGI app ourselves with a
``receive`` that delivers an ``http.disconnect`` after the first body
chunk, which lets the generator close cleanly.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


def _bootstrap_event(batch_id: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "p"},
        "data": {"n_total_jobs": 1},
    }


async def _drive_sse_request(
    app,
    path: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Run a single SSE request against the ASGI app and disconnect early.

    Returns ``(status, headers, first_chunk)``. The disconnect is sent
    as soon as the first response body chunk arrives, which gives the
    generator a clean shutdown path. Fails the test if no chunk
    arrives within 2 seconds.
    """
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    query = path.partition("?")[2].encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path.split("?")[0],
        "raw_path": path.split("?")[0].encode(),
        "query_string": query,
        "headers": raw_headers,
        "server": ("test", 80),
        "client": ("test", 0),
        "root_path": "",
    }

    sent_disconnect = False
    first_chunk_seen = asyncio.Event()

    async def receive() -> dict:
        # Feed the initial request body immediately, then wait to send
        # the disconnect until the first response chunk arrives.
        nonlocal sent_disconnect
        if not sent_disconnect and first_chunk_seen.is_set():
            sent_disconnect = True
            return {"type": "http.disconnect"}
        if not sent_disconnect:
            # Still need to emit http.request so starlette knows the body
            # is exhausted before switching to disconnect.
            await first_chunk_seen.wait()
            sent_disconnect = True
            return {"type": "http.disconnect"}
        # Any subsequent receive after disconnect keeps returning it.
        return {"type": "http.disconnect"}

    status_code: list[int] = []
    response_headers: list[tuple[bytes, bytes]] = []
    first_chunk_bytes: list[bytes] = []

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            status_code.append(int(message["status"]))
            response_headers.extend(message.get("headers", []))
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body and not first_chunk_bytes:
                first_chunk_bytes.append(body)
                first_chunk_seen.set()

    # Run the app under a bounded timeout so a broken generator can't
    # hang the test suite.
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=5.0)
    except asyncio.CancelledError:
        pass

    assert status_code, "ASGI app never emitted http.response.start"
    return status_code[0], response_headers, b"".join(first_chunk_bytes)


# ---------------------------------------------------------------------------
# Unauthorised / malformed tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_without_token_is_401(unauthed_client):
    r = await unauthed_client.get("/api/events/stream?batch_id=b1")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_stream_with_bad_token_is_401(unauthed_client):
    r = await unauthed_client.get(
        "/api/events/stream?batch_id=b1",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_stream_token_via_query_param_ok(client):
    """Browser EventSource can't set headers — query-param must work."""
    batch_id = "sse-qp-" + uuid.uuid4().hex[:8]
    # Create the batch so visibility passes.
    assert (
        await client.post("/api/events", json=_bootstrap_event(batch_id))
    ).status_code == 200

    # Reach into the test client for the underlying ASGI app so we can
    # drive the stream ourselves (httpx's ASGITransport buffers the whole
    # body which won't terminate for SSE).
    app = client._transport.app  # type: ignore[attr-defined]
    token = getattr(client, "_test_default_token")
    status_code, headers, first_chunk = await _drive_sse_request(
        app, f"/api/events/stream?batch_id={batch_id}&token={token}"
    )
    assert status_code == 200
    header_map = {k.decode(): v.decode() for k, v in headers}
    assert header_map.get("content-type", "").startswith("text/event-stream")
    assert first_chunk.startswith(b"event: hello")


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


async def _mk_user_and_token(client, username: str) -> str:
    """Register a fresh non-admin user and return their reporter token."""
    await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
    )
    jwt = lr.json()["access_token"]
    tr = await client.post(
        "/api/tokens",
        json={"name": f"{username}-rep", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    return tr.json()["token"]


@pytest.mark.asyncio
async def test_stream_foreign_batch_is_403(client):
    """A user cannot subscribe to a batch they don't own (no share yet)."""
    # Default client (tester, admin=first user) creates a batch.
    batch_id = "sse-foreign-" + uuid.uuid4().hex[:8]
    assert (
        await client.post("/api/events", json=_bootstrap_event(batch_id))
    ).status_code == 200

    # Bob — non-admin, unrelated user — tries to subscribe.
    bob_token = await _mk_user_and_token(client, "bob_sse_auth")

    r = await client.get(
        f"/api/events/stream?batch_id={batch_id}",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    # Batch exists but tester owns it and no share exists → tester is
    # admin so tester is allowed; bob is not.
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_stream_unknown_batch_is_403(client):
    """Non-existent batches also fail visibility (not 404, to avoid leaks)."""
    bob_token = await _mk_user_and_token(client, "bob_sse_unknown")
    r = await client.get(
        "/api/events/stream?batch_id=does-not-exist",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_firehose_subscription_rejected(client):
    """Non-admin cannot subscribe without a batch_id filter."""
    bob_token = await _mk_user_and_token(client, "bob_sse_firehose")
    r = await client.get(
        "/api/events/stream",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert r.status_code == 403
    assert "batch_id" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_admin_firehose_ok(client):
    """Admins may subscribe to the full firehose (no batch_id)."""
    # Default tester is admin (first registered user).
    app = client._transport.app  # type: ignore[attr-defined]
    token = getattr(client, "_test_default_token")
    status_code, _, first_chunk = await _drive_sse_request(
        app,
        f"/api/events/stream?token={token}",
    )
    assert status_code == 200
    assert first_chunk.startswith(b"event: hello")
