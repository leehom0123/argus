"""Tests for CSV export endpoints.

Covers:
  * ``GET /api/batches/{id}/export.csv``
  * ``GET /api/projects/{project}/export.csv``
  * ``GET /api/projects/{project}/export-raw.csv``
  * ``GET /api/compare/export.csv?batches=...``
"""
from __future__ import annotations

import io
import csv

import pytest

from backend.tests._dashboard_helpers import (
    mk_user_with_token,
    post_event,
    make_batch_start,
    make_job_start,
    make_job_done,
    seed_completed_batch,
)


def _parse_csv(body: bytes) -> list[list[str]]:
    text = body.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    return list(reader)


@pytest.mark.asyncio
async def test_batch_export_includes_standard_metrics(client):
    """Single batch CSV has MSE/MAE columns + one data row."""
    await seed_completed_batch(
        client,
        batch_id="b-1",
        metrics={"MSE": 0.25, "MAE": 0.31, "RMSE": 0.5},
    )
    r = await client.get("/api/batches/b-1/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = _parse_csv(r.content)
    assert rows[0][0] == "batch_id"
    assert "MSE" in rows[0]
    assert "MAE" in rows[0]
    assert len(rows) >= 2  # header + at least one job


@pytest.mark.asyncio
async def test_batch_export_404_for_invisible(client):
    await seed_completed_batch(client, batch_id="admin-only")
    bob_jwt, _ = await mk_user_with_token(client, "bob")
    r = await client.get(
        "/api/batches/admin-only/export.csv",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_project_export_leaderboard_csv(client):
    """Project leaderboard CSV has one row per (model, dataset) best."""
    batch_id = "b-lb"
    await post_event(
        client, make_batch_start(batch_id, project="p-csv", n_total=2)
    )
    for i, (model, dataset, mse) in enumerate([
        ("transformer", "etth1", 0.7),
        ("transformer", "etth1", 0.2),  # same pair, better mse
        ("autoformer", "etth2", 0.4),
    ]):
        jid = f"jcsv-{i}"
        await post_event(
            client,
            make_job_start(
                batch_id, jid, project="p-csv", model=model, dataset=dataset
            ),
        )
        await post_event(
            client,
            make_job_done(
                batch_id, jid, project="p-csv",
                elapsed_s=10, metrics={"MSE": mse},
            ),
        )

    r = await client.get("/api/projects/p-csv/export.csv")
    assert r.status_code == 200
    rows = _parse_csv(r.content)
    # header + 2 data rows (one per distinct (model, dataset))
    assert rows[0][0] == "batch_id"
    assert len(rows) == 3
    # best MSE for transformer+etth1 should be 0.2
    pair = [r for r in rows[1:] if r[1] == "transformer"]
    assert len(pair) == 1
    mse_col = rows[0].index("MSE")
    assert float(pair[0][mse_col]) == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_project_export_raw_csv(client):
    """Raw CSV has one row per (job × metric)."""
    await seed_completed_batch(
        client,
        batch_id="b-raw",
        project="p-raw",
        metrics={"MSE": 0.1, "MAE": 0.2, "RMSE": 0.3},
    )
    r = await client.get("/api/projects/p-raw/export-raw.csv")
    assert r.status_code == 200
    rows = _parse_csv(r.content)
    header = rows[0]
    assert "metric" in header
    assert "value" in header
    # 3 metrics → 3 data rows + header
    assert len(rows) == 4
    metric_names = {row[header.index("metric")] for row in rows[1:]}
    assert metric_names == {"MSE", "MAE", "RMSE"}


@pytest.mark.asyncio
async def test_compare_export_csv(client):
    """Compare CSV unions metric columns across 2 batches."""
    await seed_completed_batch(
        client, batch_id="b-1", metrics={"MSE": 0.1, "MAE": 0.2}
    )
    await seed_completed_batch(
        client, batch_id="b-2", metrics={"MSE": 0.3, "PCC": 0.8}
    )
    r = await client.get("/api/compare/export.csv?batches=b-1,b-2")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    rows = _parse_csv(r.content)
    header = rows[0]
    # Union of MSE, MAE, PCC all present
    for col in ("MSE", "MAE", "PCC"):
        assert col in header
    # 2 data rows (one per batch; each batch has 1 done job in seed helper)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_compare_export_rejects_too_many(client):
    """Past MAX_COMPARE_BATCHES (bumped to 32 in #19) the export 400s."""
    from backend.schemas.compare import MAX_COMPARE_BATCHES

    ids = ",".join(f"b{i}" for i in range(MAX_COMPARE_BATCHES + 1))
    r = await client.get(f"/api/compare/export.csv?batches={ids}")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_project_export_404_for_unknown(client):
    r = await client.get("/api/projects/nope/export.csv")
    assert r.status_code == 404
    r = await client.get("/api/projects/nope/export-raw.csv")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_csv_exports_require_auth(unauthed_client):
    for url in [
        "/api/batches/x/export.csv",
        "/api/projects/p/export.csv",
        "/api/projects/p/export-raw.csv",
        "/api/compare/export.csv?batches=a,b",
    ]:
        r = await unauthed_client.get(url)
        assert r.status_code == 401, url
