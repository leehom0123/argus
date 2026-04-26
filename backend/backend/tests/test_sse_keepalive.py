"""Keepalive heartbeat on the SSE stream.

When no real events are arriving for ``KEEPALIVE_INTERVAL_S`` the
generator must emit a ``keepalive`` frame so intermediate proxies
(nginx, cloudflare, etc.) don't time out an idle TCP socket.

To keep test runs fast we monkeypatch ``KEEPALIVE_INTERVAL_S`` down to a
fraction of a second.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.tests.test_sse_auth import _drive_sse_request


@pytest.mark.asyncio
async def test_keepalive_emitted_when_idle(client, monkeypatch):
    """With no real events, we should see ``event: keepalive`` in the stream."""
    from backend.api import events_stream as stream_mod

    # Compress the keepalive interval so the test finishes quickly.
    monkeypatch.setattr(stream_mod, "KEEPALIVE_INTERVAL_S", 0.3)

    app = client._transport.app  # type: ignore[attr-defined]
    token = getattr(client, "_test_default_token")

    # Capture multiple chunks instead of just the first — hello lands
    # immediately, keepalive should follow after the (compressed) interval.
    raw_headers = [(b"host", b"test")]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/events/stream",
        "raw_path": b"/api/events/stream",
        "query_string": f"token={token}".encode(),
        "headers": raw_headers,
        "server": ("test", 80),
        "client": ("test", 0),
        "root_path": "",
    }

    chunks: list[bytes] = []
    got_keepalive = asyncio.Event()
    disconnect_now = asyncio.Event()

    async def receive() -> dict:
        await disconnect_now.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                chunks.append(body)
                if b"event: keepalive" in b"".join(chunks):
                    got_keepalive.set()
                    disconnect_now.set()

    # Run the app under a bounded timeout. 2.0s is well above
    # (0.3s keepalive) + (0.5s generator poll step) + slack.
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=3.0)
    except asyncio.CancelledError:
        pass

    assert got_keepalive.is_set(), (
        "expected a keepalive frame within the compressed interval; "
        f"got chunks: {chunks!r}"
    )
    blob = b"".join(chunks)
    # Must start with hello, then contain keepalive.
    assert blob.startswith(b"event: hello")
    assert b"event: keepalive" in blob
