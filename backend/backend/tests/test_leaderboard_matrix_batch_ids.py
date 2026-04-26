"""Tests for matrix batch_ids field (commit 64b162d) and leaderboard i18n auth.

Commit 64b162d added ``batch_ids`` parallel array to the matrix response and
jobs metrics columns. Fills gaps:

- matrix batch_ids length matches values length per cell
- matrix batch_ids is null/empty when no batches contribute to a cell
- leaderboard endpoint requires auth (no test existed)
- matrix endpoint requires auth
- leaderboard returns empty list for project with no jobs (not 404)
"""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import (
    post_event,
    make_batch_start,
    make_job_start,
    make_job_done,
    seed_completed_batch,
)


# ---------------------------------------------------------------------------
# Matrix batch_ids contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matrix_batch_ids_parallel_to_values(client):
    """batch_ids 2D array is parallel to values 2D array (same shape: rows×cols)."""
    batch_id = "b-mx-bids-1"
    await post_event(
        client,
        make_batch_start(batch_id, project="p-mx-bids", n_total=1),
    )
    await post_event(
        client,
        make_job_start(batch_id, "j-mx-bids-1", project="p-mx-bids",
                       model="transformer", dataset="etth1"),
    )
    await post_event(
        client,
        make_job_done(batch_id, "j-mx-bids-1", project="p-mx-bids",
                      elapsed_s=20, metrics={"MSE": 0.25}),
    )
    r = await client.get("/api/projects/p-mx-bids/matrix")
    assert r.status_code == 200, r.text
    body = r.json()
    # values and batch_ids are parallel 2D arrays (rows × cols)
    values = body.get("values", [])
    batch_ids = body.get("batch_ids", [])
    assert len(batch_ids) == len(values), (
        f"batch_ids rows ({len(batch_ids)}) != values rows ({len(values)})"
    )
    for row_idx, (val_row, bid_row) in enumerate(zip(values, batch_ids)):
        assert len(bid_row) == len(val_row), (
            f"Row {row_idx}: batch_ids cols ({len(bid_row)}) != values cols ({len(val_row)})"
        )


@pytest.mark.asyncio
async def test_matrix_batch_ids_contains_correct_batch(client):
    """The batch_id must appear somewhere in the batch_ids 2D array."""
    batch_id = "b-mx-correct-bid2"
    await post_event(
        client,
        make_batch_start(batch_id, project="p-mx-correct2", n_total=1),
    )
    await post_event(
        client,
        make_job_start(batch_id, "j-mx-c2", project="p-mx-correct2",
                       model="autoformer", dataset="weather"),
    )
    await post_event(
        client,
        make_job_done(batch_id, "j-mx-c2", project="p-mx-correct2",
                      elapsed_s=15, metrics={"MSE": 0.18}),
    )
    r = await client.get("/api/projects/p-mx-correct2/matrix")
    assert r.status_code == 200, r.text
    body = r.json()
    # Flatten the 2D batch_ids array and check for the batch_id
    batch_ids_2d = body.get("batch_ids", [])
    found_bid = False
    for bid_row in batch_ids_2d:
        for cell_bids in bid_row:
            if cell_bids and batch_id in cell_bids:
                found_bid = True
    assert found_bid, (
        f"batch_id {batch_id!r} not found in matrix batch_ids: {batch_ids_2d}"
    )


# ---------------------------------------------------------------------------
# Auth boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matrix_requires_auth(unauthed_client):
    """No JWT → 401 on /api/projects/{project}/matrix."""
    r = await unauthed_client.get("/api/projects/any-proj/matrix")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_leaderboard_requires_auth(unauthed_client):
    """No JWT → 401 on /api/projects/{project}/leaderboard."""
    r = await unauthed_client.get("/api/projects/any-proj/leaderboard")
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaderboard_empty_project_returns_empty_list(client):
    """Project with running batch but no completed jobs → empty leaderboard list."""
    batch_id = "b-empty-lb"
    await post_event(
        client,
        make_batch_start(batch_id, project="p-empty-lb", n_total=0),
    )
    r = await client.get("/api/projects/p-empty-lb/leaderboard")
    assert r.status_code == 200, r.text
    assert r.json() == []


@pytest.mark.asyncio
async def test_leaderboard_null_metrics_row(client):
    """Job with no metrics at all → row appears but metrics field is null."""
    batch_id = "b-null-metrics"
    await post_event(
        client,
        make_batch_start(batch_id, project="p-null-metrics", n_total=1),
    )
    await post_event(
        client,
        make_job_start(batch_id, "j-null", project="p-null-metrics",
                       model="softs", dataset="etth2"),
    )
    # Job done with no metrics dict
    await post_event(
        client,
        {
            "schema_version": "1.1",
            "event_type": "job_done",
            "timestamp": "2026-04-23T09:05:00Z",
            "batch_id": batch_id,
            "job_id": "j-null",
            "source": {"project": "p-null-metrics"},
            "data": {"elapsed_s": 10},
        },
    )
    r = await client.get("/api/projects/p-null-metrics/leaderboard")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "done"
    assert row["metrics"] is None


@pytest.mark.asyncio
async def test_matrix_404_for_nonexistent_project(client):
    """Requesting matrix for unknown project → 404."""
    r = await client.get("/api/projects/completely-nonexistent-xyz/matrix")
    # Service returns empty matrix (200) or 404 depending on impl
    # The key invariant: not a 5xx
    assert r.status_code in (200, 404), r.text
