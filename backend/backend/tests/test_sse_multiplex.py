"""Multiplexed SSE endpoint — ``GET /api/sse?channels=...``.

Covers the new connection-merging endpoint introduced in v0.2. Following
the pattern set by :mod:`test_sse_basic`, the unit-flavoured tests poke
the hub + parsing helpers directly so they stay deterministic; the HTTP
behaviour (auth, status, ``hello`` framing) is checked through the same
``_drive_sse_request`` helper used by :mod:`test_sse_auth` so we share
one reusable ASGI driver.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi import HTTPException

from backend.api import sse_multiplex as mux
from backend.api.sse_multiplex import (
    Channel,
    _channel_matches,
    _parse_channels,
)
from backend.services import sse_hub as hub_mod
from backend.tests.test_sse_auth import _drive_sse_request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _job_epoch_event(batch_id: str, job_id: str, epoch: int = 1) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_epoch",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": "p"},
        "data": {"epoch": epoch, "train_loss": 0.5},
    }


def _parse_frame(blob: bytes) -> tuple[str, dict]:
    """Split one SSE frame ``event: X\\ndata: {...}\\n\\n`` into pieces."""
    text = blob.decode()
    lines = [ln for ln in text.splitlines() if ln]
    event = next(
        ln.split(": ", 1)[1] for ln in lines if ln.startswith("event: ")
    )
    data = next(ln.split(": ", 1)[1] for ln in lines if ln.startswith("data: "))
    return event, json.loads(data)


# ---------------------------------------------------------------------------
# _parse_channels
# ---------------------------------------------------------------------------


def test_parse_channels_accepts_all_three_kinds():
    parsed = _parse_channels("batch:b1,job:b1:j2,dashboard")
    assert [c.kind for c in parsed] == ["batch", "job", "dashboard"]
    assert parsed[0].batch_id == "b1"
    assert parsed[1].batch_id == "b1" and parsed[1].job_id == "j2"
    assert parsed[2].raw == "dashboard"


def test_parse_channels_dedupes_and_skips_blanks():
    """Trailing commas + duplicate selectors collapse to one entry each."""
    parsed = _parse_channels("batch:b1, ,batch:b1,batch:b2")
    raws = [c.raw for c in parsed]
    assert raws == ["batch:b1", "batch:b2"]


def test_parse_channels_rejects_unknown_kind():
    with pytest.raises(HTTPException) as exc:
        _parse_channels("foo:b1")
    assert exc.value.status_code == 400


def test_parse_channels_rejects_empty_input():
    with pytest.raises(HTTPException) as exc:
        _parse_channels("")
    assert exc.value.status_code == 400


def test_parse_channels_rejects_missing_batch_id():
    with pytest.raises(HTTPException) as exc:
        _parse_channels("batch:")
    assert exc.value.status_code == 400


def test_parse_channels_rejects_short_job_selector():
    with pytest.raises(HTTPException) as exc:
        _parse_channels("job:onlybatch")
    assert exc.value.status_code == 400


def test_parse_channels_caps_count(monkeypatch):
    monkeypatch.setattr(mux, "MAX_CHANNELS_PER_CONNECTION", 2)
    with pytest.raises(HTTPException) as exc:
        _parse_channels("batch:a,batch:b,batch:c")
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# _channel_matches
# ---------------------------------------------------------------------------


def test_channel_matches_batch():
    ch = Channel(kind="batch", raw="batch:b1", batch_id="b1")
    assert _channel_matches(ch, {"batch_id": "b1"})
    assert not _channel_matches(ch, {"batch_id": "b2"})


def test_channel_matches_job_requires_both_ids():
    ch = Channel(kind="job", raw="job:b1:j2", batch_id="b1", job_id="j2")
    assert _channel_matches(ch, {"batch_id": "b1", "job_id": "j2"})
    # Same batch, different job: no match.
    assert not _channel_matches(ch, {"batch_id": "b1", "job_id": "j9"})
    # Job-less event (e.g. batch_done) does not flow into a job channel.
    assert not _channel_matches(ch, {"batch_id": "b1"})


def test_channel_matches_dashboard_is_firehose():
    ch = Channel(kind="dashboard", raw="dashboard")
    assert _channel_matches(ch, {"batch_id": "anything"})
    assert _channel_matches(ch, {"event_type": "batch_done"})


# ---------------------------------------------------------------------------
# Demux semantics — only frames matching the channel get tagged + emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_routes_only_matching_frames_to_channels():
    """Subscribe to two channels; assert each only sees its own frames."""
    hub = hub_mod.SSEHub()
    sid_a, q_a = await hub.subscribe({"batch_id": "batch-A"})
    sid_b, q_b = await hub.subscribe({"batch_id": "batch-B"})

    # Publish one event per batch.
    await hub.publish(_bootstrap_event("batch-A"))
    await hub.publish(_bootstrap_event("batch-B"))

    ev_a = await asyncio.wait_for(q_a.get(), timeout=0.5)
    ev_b = await asyncio.wait_for(q_b.get(), timeout=0.5)
    assert ev_a["batch_id"] == "batch-A"
    assert ev_b["batch_id"] == "batch-B"
    assert q_a.empty()
    assert q_b.empty()

    await hub.unsubscribe(sid_a)
    await hub.unsubscribe(sid_b)


@pytest.mark.asyncio
async def test_job_channel_filters_by_job_id_post_hub():
    """Hub returns all batch traffic; channel match keeps only the job's."""
    ch = Channel(kind="job", raw="job:B:J1", batch_id="B", job_id="J1")
    delivered = [
        _job_epoch_event("B", "J1", 1),
        _job_epoch_event("B", "J2", 1),  # different job — must not match
        _job_epoch_event("B", "J1", 2),
    ]
    matches = [e for e in delivered if _channel_matches(ch, e)]
    assert [e["job_id"] for e in matches] == ["J1", "J1"]


@pytest.mark.asyncio
async def test_event_matching_two_channels_emits_one_frame_per_channel():
    """An event belonging to both 'batch:B' and 'job:B:J1' is emitted twice.

    Justification: frontend demuxes by ``channel`` field; if the two
    channels feed two independent UI surfaces we must give each a copy
    rather than guessing which surface to update.
    """
    chs = [
        Channel(kind="batch", raw="batch:B", batch_id="B"),
        Channel(kind="job", raw="job:B:J1", batch_id="B", job_id="J1"),
    ]
    ev = _job_epoch_event("B", "J1")
    matches = [c.raw for c in chs if _channel_matches(c, ev)]
    assert matches == ["batch:B", "job:B:J1"]


# ---------------------------------------------------------------------------
# HTTP-level: validation + auth + happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_rejects_missing_channels(unauthed_client):
    """No channels query → 422 (FastAPI's required-field validator)."""
    r = await unauthed_client.get("/api/sse")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_rejects_no_token(unauthed_client):
    r = await unauthed_client.get("/api/sse?channels=batch:b1")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_rejects_unknown_channel_kind(client):
    r = await client.get("/api/sse?channels=foo:b1")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_dashboard_admin_only(client):
    """Dashboard channel inherits the admin-only firehose rule."""
    from backend.tests.test_sse_auth import _mk_user_and_token

    bob_token = await _mk_user_and_token(client, "bob_mux_dash")
    r = await client.get(
        "/api/sse?channels=dashboard",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_unauthorised_batch_is_403(client):
    """A non-admin caller cannot subscribe to a batch they can't see."""
    from backend.tests.test_sse_auth import _mk_user_and_token

    batch_id = "mux-foreign-" + uuid.uuid4().hex[:8]
    assert (
        await client.post("/api/events", json=_bootstrap_event(batch_id))
    ).status_code == 200
    bob_token = await _mk_user_and_token(client, "bob_mux_foreign")
    r = await client.get(
        f"/api/sse?channels=batch:{batch_id}",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_partial_visibility_returns_403_not_silent_drop(client):
    """Reviewer follow-up: ``_enforce_channel_visibility`` must short-circuit
    on the first forbidden channel rather than silently dropping it from
    the delivery set.

    Setup: bob owns ``batch:X`` (via his reporter token); admin owns
    ``batch:Y``. Bob asks for ``channels=batch:X,batch:Y``.

    Expected: HTTP 403 — the connection is rejected outright. The wrong
    behaviour would be HTTP 200 followed by frames for X only (silent
    drop), which would let a caller mask which channels they were
    actually allowed to see.
    """
    from backend.tests.test_sse_auth import _mk_user_and_token

    # Admin (default client) creates batch Y — bob has no access.
    batch_y = "mux-partial-y-" + uuid.uuid4().hex[:8]
    assert (
        await client.post("/api/events", json=_bootstrap_event(batch_y))
    ).status_code == 200

    # Bob registers + creates batch X with his own reporter token, so
    # the token-binding migration (#127) records bob as the owner.
    bob_token = await _mk_user_and_token(client, "bob_mux_partial")
    batch_x = "mux-partial-x-" + uuid.uuid4().hex[:8]
    assert (
        await client.post(
            "/api/events",
            json=_bootstrap_event(batch_x),
            headers={"Authorization": f"Bearer {bob_token}"},
        )
    ).status_code == 200

    # Partial visibility: X allowed, Y forbidden → must reject the
    # whole connection, not silently drop Y. (We can't sanity-check
    # the X-only case with httpx AsyncClient because a successful SSE
    # subscribe streams indefinitely; the visibility unit tests
    # already cover the X-only path via _enforce_subscribe_visibility.)
    r = await client.get(
        f"/api/sse?channels=batch:{batch_x},batch:{batch_y}",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert r.status_code == 403
    # Detail comes from the same i18n key the legacy endpoint uses, so
    # a caller can't tell "you don't own Y" apart from "you don't own X"
    # — but it must be a denial message, not a stream payload.
    detail = r.json().get("detail", "")
    assert "access" in detail.lower()


@pytest.mark.asyncio
async def test_endpoint_hello_frame_lists_channels(client):
    """Happy path: connect, see the ``hello`` frame echoes the selector set."""
    batch_id = "mux-hello-" + uuid.uuid4().hex[:8]
    assert (
        await client.post("/api/events", json=_bootstrap_event(batch_id))
    ).status_code == 200

    app = client._transport.app  # type: ignore[attr-defined]
    token = getattr(client, "_test_default_token")
    status_code, headers, first_chunk = await _drive_sse_request(
        app, f"/api/sse?channels=batch:{batch_id}&token={token}"
    )
    assert status_code == 200
    header_map = {k.decode(): v.decode() for k, v in headers}
    assert header_map.get("content-type", "").startswith("text/event-stream")
    assert first_chunk.startswith(b"event: hello")
    event, payload = _parse_frame(first_chunk)
    assert event == "hello"
    assert payload["channels"] == [f"batch:{batch_id}"]
    assert payload["subscribed"] is True


@pytest.mark.asyncio
async def test_endpoint_unsubscribes_on_disconnect(client):
    """Tearing down the connection unsubscribes from the hub.

    Each channel registers one hub subscription, so a multiplex with N
    channels should leave the hub with 0 live subs after disconnect.
    """
    hub = hub_mod.SSEHub()
    original = hub_mod.hub
    hub_mod.hub = hub
    try:
        batch_id = "mux-disc-" + uuid.uuid4().hex[:8]
        assert (
            await client.post("/api/events", json=_bootstrap_event(batch_id))
        ).status_code == 200

        assert hub._subscription_count() == 0
        app = client._transport.app  # type: ignore[attr-defined]
        token = getattr(client, "_test_default_token")
        status_code, _, chunk = await _drive_sse_request(
            app,
            f"/api/sse?channels=batch:{batch_id}&token={token}",
        )
        assert status_code == 200
        assert chunk.startswith(b"event: hello")
        # Generator's ``finally`` runs on disconnect; allow one tick for
        # the forwarder cancellation to settle.
        await asyncio.sleep(0.1)
        assert hub._subscription_count() == 0
    finally:
        hub_mod.hub = original
