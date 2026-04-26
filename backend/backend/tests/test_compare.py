"""Tests for ``/api/compare`` — side-by-side batch comparison."""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import (
    mk_user_with_token,
    seed_completed_batch,
)


@pytest.mark.asyncio
async def test_compare_two_batches(client):
    """2 visible batches → 2-column response."""
    await seed_completed_batch(
        client, batch_id="b-1", metrics={"MSE": 0.1, "MAE": 0.2}
    )
    await seed_completed_batch(
        client, batch_id="b-2", metrics={"MSE": 0.3, "MAE": 0.4}
    )
    r = await client.get("/api/compare?batches=b-1,b-2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["batches"]) == 2
    ids = [col["batch_id"] for col in body["batches"]]
    assert ids == ["b-1", "b-2"]
    assert "MSE" in body["metric_union"]
    assert "MAE" in body["metric_union"]


@pytest.mark.asyncio
async def test_compare_rejects_under_two(client):
    """<2 ids → 400."""
    await seed_completed_batch(client, batch_id="b-1")
    r = await client.get("/api/compare?batches=b-1")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_compare_rejects_over_cap(client):
    """>MAX_COMPARE_BATCHES (32) → 400 at the validation step."""
    # Issue #19 bumped the cap from 4 to 32. Build a 33-id list.
    ids = ",".join(f"b{i}" for i in range(33))
    r = await client.get(f"/api/compare?batches={ids}")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_compare_404_for_invisible_batch(client):
    """One invisible batch in the list → entire request 404s."""
    await seed_completed_batch(client, batch_id="admin-owned")

    # Bob can't see admin-owned.
    bob_jwt, bob_token = await mk_user_with_token(client, "bob")
    await seed_completed_batch(
        client,
        batch_id="bob-owned",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    r = await client.get(
        "/api/compare?batches=admin-owned,bob-owned",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_compare_best_metric_is_lowest_mse(client):
    """best_metric tracks the lowest MSE across the batch's done jobs."""
    from backend.tests._dashboard_helpers import (
        post_event,
        make_batch_start,
        make_job_start,
        make_job_done,
    )

    batch_id = "b-mix"
    await post_event(
        client, make_batch_start(batch_id, n_total=3)
    )
    for i, mse in enumerate([0.9, 0.2, 0.5]):
        jid = f"j-{i}"
        await post_event(
            client, make_job_start(batch_id, jid)
        )
        await post_event(
            client,
            make_job_done(
                batch_id, jid, elapsed_s=10, metrics={"MSE": mse}
            ),
        )
    await seed_completed_batch(client, batch_id="b-other")

    r = await client.get(f"/api/compare?batches={batch_id},b-other")
    assert r.status_code == 200
    col = next(c for c in r.json()["batches"] if c["batch_id"] == batch_id)
    assert col["best_metric"]["value"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_compare_requires_auth(unauthed_client):
    r = await unauthed_client.get("/api/compare?batches=a,b")
    assert r.status_code == 401
