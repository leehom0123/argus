"""Tests for ``/api/compare`` with N > 4 (issue #19).

The cap used to be 4 batches (MVP UX constraint). Project-wide sweep
analysis needs to compare more — up to 32 now. These tests lock in the
new behaviour:

* 5..32 batches: 200 OK with N columns.
* 33+ batches: 400 at validation.
* Existing 2..4 contract: unchanged.
"""
from __future__ import annotations

import pytest

from backend.schemas.compare import MAX_COMPARE_BATCHES
from backend.tests._dashboard_helpers import seed_completed_batch


@pytest.mark.asyncio
async def test_compare_accepts_five_batches(client):
    """5 ids → 200 with 5 columns (previously 400 under the old 4-cap)."""
    for i in range(5):
        await seed_completed_batch(
            client, batch_id=f"b-{i}", metrics={"MSE": 0.1 + 0.1 * i}
        )
    r = await client.get(
        "/api/compare?batches=" + ",".join(f"b-{i}" for i in range(5))
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["batches"]) == 5
    ids = [col["batch_id"] for col in body["batches"]]
    assert ids == [f"b-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_compare_accepts_eight_batches_with_metric_union(client):
    """8 batches with different metric keys merge into the union."""
    for i in range(8):
        # Alternate MAE / RMSE so the union covers several keys.
        extra = {"MAE": float(i)} if i % 2 == 0 else {"RMSE": float(i)}
        await seed_completed_batch(
            client, batch_id=f"b-{i}", metrics={"MSE": 0.1 + i, **extra}
        )
    r = await client.get(
        "/api/compare?batches=" + ",".join(f"b-{i}" for i in range(8))
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["batches"]) == 8
    # Every metric key that appeared in any batch shows up in the union.
    assert set(body["metric_union"]) >= {"MSE", "MAE", "RMSE"}


@pytest.mark.asyncio
async def test_compare_at_exact_cap(client):
    """Exactly MAX_COMPARE_BATCHES (32) succeeds.

    Seeding 32 batches via the event API would blow the default rate
    limit bucket (capacity 60, we'd need 96 events). Reset the bucket
    a few times mid-seed so this isn't a rate-limit test.
    """
    from backend.utils.ratelimit import reset_default_bucket_for_tests

    ids = [f"b-{i}" for i in range(MAX_COMPARE_BATCHES)]
    for n, bid in enumerate(ids):
        if n and n % 10 == 0:
            reset_default_bucket_for_tests()
        await seed_completed_batch(client, batch_id=bid)
    r = await client.get("/api/compare?batches=" + ",".join(ids))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["batches"]) == MAX_COMPARE_BATCHES


@pytest.mark.asyncio
async def test_compare_one_over_cap_rejects(client):
    """MAX_COMPARE_BATCHES + 1 → 400 with an informative message."""
    ids = ",".join(f"b-{i}" for i in range(MAX_COMPARE_BATCHES + 1))
    r = await client.get(f"/api/compare?batches={ids}")
    assert r.status_code == 400
    # Error message mentions the cap so the UI can render a precise hint.
    detail = r.json()["detail"]
    assert "32" in detail or "max" in detail.lower()


@pytest.mark.asyncio
async def test_compare_csv_export_handles_n_gt_4(client):
    """CSV export under the new cap: one row per (batch, job)."""
    for i in range(6):
        await seed_completed_batch(
            client, batch_id=f"b-{i}", metrics={"MSE": 0.1 + i}
        )
    r = await client.get(
        "/api/compare/export.csv?batches="
        + ",".join(f"b-{i}" for i in range(6))
    )
    assert r.status_code == 200
    text = r.text
    # Header + 6 data rows (each batch has one completed job in the fixture).
    lines = [ln for ln in text.strip().split("\n") if ln]
    assert len(lines) == 7
    for i in range(6):
        assert f"b-{i}" in text
