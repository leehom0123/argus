"""Integration tests for ``GET /api/jobs/{batch_id}/{job_id}/log-lines``.

The endpoint is the job-scoped sibling of
``GET /api/batches/{id}/log-lines`` — same shape, with the additional
``since=<event_id>`` cursor + a 404 contract when the job doesn't
belong to the batch. The JobDetail Logs tab uses it to fill the
buffer before opening the SSE log stream.
"""
from __future__ import annotations

import uuid

import pytest

from backend.tests._dashboard_helpers import (
    make_batch_start,
    make_job_start,
    post_event,
)


def _make_log_line(
    batch_id: str,
    job_id: str,
    line: str = "step 1",
    level: str = "info",
    ts: str = "2026-04-23T09:01:00Z",
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "log_line",
        "timestamp": ts,
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": "deepts"},
        "data": {"level": level, "line": line},
    }


async def _seed_job(client, batch_id: str, job_id: str) -> None:
    await post_event(client, make_batch_start(batch_id))
    await post_event(client, make_job_start(batch_id, job_id))


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_lines_filters_to_one_job(client) -> None:
    """Rows for other jobs in the same batch are excluded."""
    batch_id = "jl-batch-1"
    job_a = "j-a"
    job_b = "j-b"

    await _seed_job(client, batch_id, job_a)
    await post_event(client, make_job_start(batch_id, job_b, ts="2026-04-23T09:01:30Z"))

    await post_event(
        client,
        _make_log_line(batch_id, job_a, line="from a", ts="2026-04-23T09:02:00Z"),
    )
    await post_event(
        client,
        _make_log_line(batch_id, job_b, line="from b", ts="2026-04-23T09:02:10Z"),
    )
    await post_event(
        client,
        _make_log_line(batch_id, job_a, line="also a", ts="2026-04-23T09:02:20Z"),
    )

    r = await client.get(f"/api/jobs/{batch_id}/{job_a}/log-lines")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 2
    # Every row must be for job_a.
    assert all(row["job_id"] == job_a for row in body)
    # And the payload must include the line text we posted.
    lines = sorted(row["line"] for row in body)
    assert lines == ["also a", "from a"]
    # Shape: id + event_id + ts + level + line + job_id
    sample = body[0]
    assert set(sample.keys()) == {
        "id", "event_id", "ts", "level", "line", "job_id",
    }
    assert isinstance(sample["id"], int)


@pytest.mark.asyncio
async def test_job_log_lines_since_returns_only_newer(client) -> None:
    """``since=<event_id>`` excludes rows with id <= since."""
    batch_id = "jl-batch-2"
    job_id = "j-1"

    await _seed_job(client, batch_id, job_id)

    # Post 3 log lines so we get at least 3 distinct event ids.
    for i in range(3):
        await post_event(
            client,
            _make_log_line(
                batch_id,
                job_id,
                line=f"line {i}",
                ts=f"2026-04-23T09:0{i}:00Z",
            ),
        )

    # Bypass the response cache so the second call doesn't reuse the
    # pre-since payload.
    r1 = await client.get(
        f"/api/jobs/{batch_id}/{job_id}/log-lines",
        params={"bust": "1"},
    )
    assert r1.status_code == 200
    rows = r1.json()
    assert len(rows) == 3
    ids = sorted(row["id"] for row in rows)
    cursor = ids[0]  # the lowest id we saw

    r2 = await client.get(
        f"/api/jobs/{batch_id}/{job_id}/log-lines",
        params={"since": cursor, "bust": "2"},
    )
    assert r2.status_code == 200
    rows2 = r2.json()
    assert len(rows2) == 2
    assert all(row["id"] > cursor for row in rows2)


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_lines_404_when_job_missing(client) -> None:
    """Unknown ``(batch_id, job_id)`` → 404."""
    batch_id = "jl-batch-3"
    await post_event(client, make_batch_start(batch_id))

    r = await client.get(f"/api/jobs/{batch_id}/no-such-job/log-lines")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_job_log_lines_requires_auth(unauthed_client) -> None:
    """No bearer token → 401 (matches the rest of /api/jobs/*)."""
    r = await unauthed_client.get("/api/jobs/any-batch/any-job/log-lines")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_lines_since_no_new_rows_returns_empty(client) -> None:
    """``since=`` past the latest row returns an empty list, not 500.

    The JobDetail SSE consumer hits this branch every time the live tail
    catches up to the buffer — it must respond with 200 + ``[]`` so the
    frontend treats it as "nothing new yet" instead of an error.
    """
    batch_id = "jl-batch-empty"
    job_id = "j-empty"

    await _seed_job(client, batch_id, job_id)
    await post_event(
        client,
        _make_log_line(batch_id, job_id, line="only line"),
    )

    r1 = await client.get(
        f"/api/jobs/{batch_id}/{job_id}/log-lines",
        params={"bust": "a"},
    )
    assert r1.status_code == 200
    rows = r1.json()
    assert len(rows) == 1
    cursor = rows[0]["id"]

    # Advance past the latest row — no new events written.
    r2 = await client.get(
        f"/api/jobs/{batch_id}/{job_id}/log-lines",
        params={"since": cursor, "bust": "b"},
    )
    assert r2.status_code == 200
    assert r2.json() == []


@pytest.mark.asyncio
async def test_job_log_lines_response_includes_event_id(client) -> None:
    """Each row carries the v1.1 client ``event_id`` UUID.

    The frontend dedup key for the live SSE viewer is ``event_id`` (the
    DB ``id`` is missing on SSE frames). If the poll endpoint omits it,
    poll-then-SSE handover double-renders every overlapping row.
    """
    batch_id = "jl-batch-eid"
    job_id = "j-eid"

    await _seed_job(client, batch_id, job_id)
    payload = _make_log_line(batch_id, job_id, line="with eid")
    await post_event(client, payload)

    r = await client.get(
        f"/api/jobs/{batch_id}/{job_id}/log-lines",
        params={"bust": "x"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    # The poll endpoint must surface event_id (UUID) so the SSE consumer
    # can dedup against poll rows. Exact value is whatever post_event sent
    # (its helper auto-fills a fresh UUID); we only assert presence + type.
    assert rows[0]["event_id"] is not None
    assert isinstance(rows[0]["event_id"], str)
    assert len(rows[0]["event_id"]) >= 32  # uuid string
    # Schema must include event_id so the frontend type contract holds.
    assert set(rows[0].keys()) == {
        "id", "event_id", "ts", "job_id", "level", "line",
    }
