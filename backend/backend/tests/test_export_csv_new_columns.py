"""Tests for leaderboard CSV export after d68d806 added status + epochs columns.

The existing test_export_csv.py tests were written before d68d806 and do not
verify that the two new columns (status, epochs) appear in the CSV output.

Covers:
- CSV header contains "status" and "epochs" columns
- A done job with train_epochs in metrics → epochs cell is populated
- A running job → status cell is "running", epochs cell is blank
- Auth boundary: export requires login
"""
from __future__ import annotations

import csv
import io

import pytest

from backend.tests._dashboard_helpers import (
    post_event,
    make_batch_start,
    make_job_start,
    make_job_done,
    seed_completed_batch,
)


def _parse_csv(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8"))))


# ---------------------------------------------------------------------------
# Header verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaderboard_csv_has_status_and_epochs_columns(client):
    """CSV header must include 'status' and 'epochs' after d68d806."""
    await seed_completed_batch(
        client,
        batch_id="b-hdr-csv",
        project="p-hdr-csv",
        metrics={"MSE": 0.25},
    )
    r = await client.get("/api/projects/p-hdr-csv/export.csv")
    assert r.status_code == 200, r.text
    rows = _parse_csv(r.content)
    header = rows[0]
    assert "status" in header, f"'status' column missing from CSV header: {header}"
    assert "epochs" in header, f"'epochs' column missing from CSV header: {header}"


# ---------------------------------------------------------------------------
# status column values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaderboard_csv_done_status(client):
    """A done job → status cell is 'done'."""
    await seed_completed_batch(
        client,
        batch_id="b-done-csv",
        project="p-done-csv",
        metrics={"MSE": 0.1},
    )
    r = await client.get("/api/projects/p-done-csv/export.csv")
    assert r.status_code == 200, r.text
    rows = _parse_csv(r.content)
    header = rows[0]
    status_col = header.index("status")
    data_row = rows[1]
    assert data_row[status_col] == "done", (
        f"Expected status='done', got {data_row[status_col]!r}"
    )


@pytest.mark.asyncio
async def test_leaderboard_csv_running_status_and_empty_epochs(client):
    """A running job (no job_done) → status='running', epochs column is blank."""
    batch_id = "b-run-csv"
    await post_event(
        client, make_batch_start(batch_id, project="p-run-csv", n_total=1)
    )
    await post_event(
        client,
        make_job_start(batch_id, "j-run-csv", project="p-run-csv",
                       model="dlinear", dataset="etth1"),
    )
    # No job_done — job is still running
    r = await client.get("/api/projects/p-run-csv/export.csv")
    assert r.status_code == 200, r.text
    rows = _parse_csv(r.content)
    header = rows[0]
    status_col = header.index("status")
    epochs_col = header.index("epochs")
    assert len(rows) >= 2, "Expected at least one data row"
    data_row = rows[1]
    assert data_row[status_col] == "running", (
        f"Expected status='running', got {data_row[status_col]!r}"
    )
    assert data_row[epochs_col] == "", (
        f"Expected empty epochs for running job, got {data_row[epochs_col]!r}"
    )


# ---------------------------------------------------------------------------
# epochs column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaderboard_csv_epochs_from_train_epochs_key(client):
    """When metrics contains train_epochs, epochs column is populated."""
    batch_id = "b-ep-csv"
    await post_event(
        client, make_batch_start(batch_id, project="p-ep-csv", n_total=1)
    )
    await post_event(
        client,
        make_job_start(batch_id, "j-ep-csv", project="p-ep-csv",
                       model="timesnet", dataset="etth2"),
    )
    await post_event(
        client,
        make_job_done(
            batch_id, "j-ep-csv", project="p-ep-csv",
            elapsed_s=60,
            metrics={"MSE": 0.3, "train_epochs": 47},
        ),
    )
    r = await client.get("/api/projects/p-ep-csv/export.csv")
    assert r.status_code == 200, r.text
    rows = _parse_csv(r.content)
    header = rows[0]
    epochs_col = header.index("epochs")
    assert rows[1][epochs_col] == "47", (
        f"Expected epochs='47', got {rows[1][epochs_col]!r}"
    )


@pytest.mark.asyncio
async def test_leaderboard_csv_epochs_from_epochs_key(client):
    """'epochs' (not 'train_epochs') in metrics is also accepted."""
    batch_id = "b-ep2-csv"
    await post_event(
        client, make_batch_start(batch_id, project="p-ep2-csv", n_total=1)
    )
    await post_event(
        client,
        make_job_start(batch_id, "j-ep2-csv", project="p-ep2-csv",
                       model="dlinear", dataset="etth1"),
    )
    await post_event(
        client,
        make_job_done(
            batch_id, "j-ep2-csv", project="p-ep2-csv",
            elapsed_s=30,
            metrics={"MSE": 0.2, "epochs": 33},
        ),
    )
    r = await client.get("/api/projects/p-ep2-csv/export.csv")
    assert r.status_code == 200, r.text
    rows = _parse_csv(r.content)
    header = rows[0]
    epochs_col = header.index("epochs")
    assert rows[1][epochs_col] == "33", (
        f"Expected epochs='33', got {rows[1][epochs_col]!r}"
    )


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaderboard_csv_requires_auth(unauthed_client):
    """No JWT → 401 on the CSV export endpoint."""
    r = await unauthed_client.get("/api/projects/any-project/export.csv")
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Empty project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaderboard_csv_unknown_project_404(client):
    """Unknown project → 404, not a blank CSV."""
    r = await client.get("/api/projects/completely-unknown-project-xyz/export.csv")
    assert r.status_code == 404, r.text
