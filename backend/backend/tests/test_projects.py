"""Tests for ``/api/projects/*`` endpoints."""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import (
    mk_user_with_token,
    post_event,
    seed_completed_batch,
    make_batch_start,
    make_job_start,
    make_job_done,
)


@pytest.mark.asyncio
async def test_list_projects_returns_visible_only(client):
    """Caller only sees projects containing visible batches."""
    await seed_completed_batch(client, batch_id="b-a", project="proj-alice")

    bob_jwt, bob_token = await mk_user_with_token(client, "bob")
    await seed_completed_batch(
        client,
        batch_id="b-b",
        project="proj-bob",
        headers={"Authorization": f"Bearer {bob_token}"},
    )

    r = await client.get(
        "/api/projects", headers={"Authorization": f"Bearer {bob_jwt}"}
    )
    assert r.status_code == 200
    names = [p["project"] for p in r.json()]
    assert "proj-bob" in names
    assert "proj-alice" not in names


@pytest.mark.asyncio
async def test_project_detail_includes_best_metric(client):
    """A done job with MSE → best_metric populated."""
    await seed_completed_batch(
        client,
        batch_id="b-1",
        project="p-1",
        metrics={"MSE": 0.11, "MAE": 0.22, "RMSE": 0.33},
    )
    r = await client.get("/api/projects/p-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"] == "p-1"
    assert body["n_batches"] == 1
    assert body["best_metric"] is not None
    assert body["best_metric"]["name"] == "MSE"
    assert body["best_metric"]["value"] == pytest.approx(0.11)
    assert body["gpu_hours"] > 0


@pytest.mark.asyncio
async def test_project_detail_404_for_unknown(client):
    r = await client.get("/api/projects/nope")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_project_leaderboard_picks_best(client):
    """Two done jobs on same (model, dataset) → leaderboard keeps the best."""
    batch_id = "b-multi"
    await post_event(
        client,
        make_batch_start(batch_id, project="p-lb", n_total=3),
    )
    # Two jobs of the same (model, dataset) — leaderboard should pick min MSE.
    await post_event(
        client,
        make_job_start(batch_id, "j-1", project="p-lb",
                       model="transformer", dataset="etth1"),
    )
    await post_event(
        client,
        make_job_done(batch_id, "j-1", project="p-lb",
                      elapsed_s=10, metrics={"MSE": 0.9}),
    )
    await post_event(
        client,
        make_job_start(batch_id, "j-2", project="p-lb",
                       model="transformer", dataset="etth1"),
    )
    await post_event(
        client,
        make_job_done(batch_id, "j-2", project="p-lb",
                      elapsed_s=12, metrics={"MSE": 0.3}),
    )

    r = await client.get("/api/projects/p-lb/leaderboard")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["model"] == "transformer"
    assert rows[0]["dataset"] == "etth1"
    assert rows[0]["best_metric"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_project_matrix_shape_matches_jobs(client):
    """Matrix rows = models, cols = datasets."""
    batch_id = "b-mat"
    await post_event(
        client,
        make_batch_start(batch_id, project="p-mat", n_total=2),
    )
    for i, (model, dataset, mse) in enumerate([
        ("transformer", "etth1", 0.2),
        ("autoformer", "etth2", 0.15),
    ]):
        job_id = f"jm-{i}"
        await post_event(
            client,
            make_job_start(batch_id, job_id, project="p-mat",
                           model=model, dataset=dataset),
        )
        await post_event(
            client,
            make_job_done(batch_id, job_id, project="p-mat",
                          elapsed_s=10, metrics={"MSE": mse}),
        )
    r = await client.get("/api/projects/p-mat/matrix")
    assert r.status_code == 200
    body = r.json()
    assert set(body["rows"]) == {"transformer", "autoformer"}
    assert set(body["cols"]) == {"etth1", "etth2"}
    assert len(body["values"]) == 2
    assert len(body["values"][0]) == 2


@pytest.mark.asyncio
async def test_project_matrix_batch_ids_parallel_to_values(client):
    """Matrix response includes batch_ids parallel matrix with contributing batch_id per cell."""
    batch_id = "b-mat-bid"
    await post_event(
        client,
        make_batch_start(batch_id, project="p-mat-bid", n_total=1),
    )
    await post_event(
        client,
        make_job_start(batch_id, "jbid-0", project="p-mat-bid",
                       model="dlinear", dataset="etth1"),
    )
    await post_event(
        client,
        make_job_done(batch_id, "jbid-0", project="p-mat-bid",
                      elapsed_s=5, metrics={"MSE": 0.3}),
    )
    r = await client.get("/api/projects/p-mat-bid/matrix")
    assert r.status_code == 200
    body = r.json()
    # batch_ids must be present and have the same shape as values.
    assert "batch_ids" in body
    assert len(body["batch_ids"]) == len(body["rows"])
    for row_bids, row_vals in zip(body["batch_ids"], body["values"]):
        assert len(row_bids) == len(row_vals)
    # The one occupied cell must reference our batch_id.
    row_idx = body["rows"].index("dlinear")
    col_idx = body["cols"].index("etth1")
    cell_bids = body["batch_ids"][row_idx][col_idx]
    assert cell_bids is not None
    assert batch_id in cell_bids


@pytest.mark.asyncio
async def test_project_active_batches_lists_running(client):
    """Active-batches tab shows running batches with running jobs listed."""
    batch_id = "b-active"
    await post_event(
        client,
        make_batch_start(batch_id, project="p-active", n_total=2),
    )
    await post_event(
        client,
        make_job_start(batch_id, "jl-1", project="p-active"),
    )
    r = await client.get("/api/projects/p-active/active-batches")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["batch_id"] == batch_id
    assert any(j["job_id"] == "jl-1" for j in rows[0]["running_jobs"])


@pytest.mark.asyncio
async def test_project_resources_accumulates_gpu_hours(client):
    """Sum of job elapsed_s / 3600 = gpu_hours."""
    await seed_completed_batch(
        client, batch_id="b-res", project="p-res", elapsed_s=3600
    )
    r = await client.get("/api/projects/p-res/resources")
    assert r.status_code == 200
    body = r.json()
    assert body["jobs_completed"] == 1
    assert body["gpu_hours"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_project_detail_blocked_for_non_visible(client):
    """Bob can't see admin-only project detail → 404."""
    await seed_completed_batch(
        client, batch_id="secret", project="secret-project"
    )
    bob_jwt, _ = await mk_user_with_token(client, "bob")
    r = await client.get(
        "/api/projects/secret-project",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_projects_require_auth(unauthed_client):
    r = await unauthed_client.get("/api/projects")
    assert r.status_code == 401
    r = await unauthed_client.get("/api/projects/any")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_leaderboard_row_includes_status_and_metrics_dict(client):
    """Done job → row has status='done' and a full metrics dict."""
    await seed_completed_batch(
        client,
        batch_id="b-new-shape",
        project="p-shape",
        metrics={"MSE": 0.15, "MAE": 0.22, "RMSE": 0.39, "R2": 0.88},
    )
    r = await client.get("/api/projects/p-shape/leaderboard")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    # Status field must be populated.
    assert row["status"] == "done"
    # best_metric reflects the sort metric (MSE by default).
    assert row["best_metric"] == pytest.approx(0.15)
    assert row["metric_name"] == "MSE"
    # Full metrics dict must be returned.
    assert row["metrics"] is not None
    assert row["metrics"]["MSE"] == pytest.approx(0.15)
    assert row["metrics"]["MAE"] == pytest.approx(0.22)
    assert row["metrics"]["RMSE"] == pytest.approx(0.39)
    assert row["metrics"]["R2"] == pytest.approx(0.88)


@pytest.mark.asyncio
async def test_leaderboard_includes_running_jobs_without_metrics(client):
    """A running job with no metrics appears in the leaderboard with status and null best_metric."""
    batch_id = "b-running"
    await post_event(client, make_batch_start(batch_id, project="p-running", n_total=1))
    await post_event(
        client,
        make_job_start(batch_id, "j-run-1", project="p-running",
                       model="patchtst", dataset="etth2"),
    )
    # No job_done — the job is still "running".
    r = await client.get("/api/projects/p-running/leaderboard")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "patchtst"
    assert row["dataset"] == "etth2"
    assert row["status"] == "running"
    assert row["best_metric"] is None
    assert row["metric_name"] is None
    assert row["metrics"] is None


@pytest.mark.asyncio
async def test_leaderboard_train_epochs_from_metrics(client):
    """When the reporter embeds train_epochs in the metrics dict, the row reflects it."""
    batch_id = "b-epochs"
    await post_event(client, make_batch_start(batch_id, project="p-epochs", n_total=1))
    await post_event(
        client,
        make_job_start(batch_id, "j-ep-1", project="p-epochs",
                       model="timesnet", dataset="etth1"),
    )
    await post_event(
        client,
        make_job_done(batch_id, "j-ep-1", project="p-epochs",
                      elapsed_s=60,
                      metrics={"MSE": 0.2, "train_epochs": 42}),
    )
    r = await client.get("/api/projects/p-epochs/leaderboard")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["train_epochs"] == 42


@pytest.mark.asyncio
async def test_project_detail_batches_this_week_recent(client):
    """A batch started 3 days ago counts toward batches_this_week."""
    await post_event(
        client,
        make_batch_start(
            "b-week-recent",
            project="p-week",
            ts="2026-04-21T12:00:00Z",  # 3 days before 2026-04-24
        ),
    )
    r = await client.get("/api/projects/p-week")
    assert r.status_code == 200, r.text
    assert r.json()["batches_this_week"] == 1


@pytest.mark.asyncio
async def test_project_detail_batches_this_week_old_excluded(client):
    """A batch started 10 days ago does NOT count toward batches_this_week."""
    await post_event(
        client,
        make_batch_start(
            "b-week-old",
            project="p-old-week",
            ts="2026-04-14T12:00:00Z",  # 10 days before 2026-04-24
        ),
    )
    r = await client.get("/api/projects/p-old-week")
    assert r.status_code == 200, r.text
    assert r.json()["batches_this_week"] == 0


@pytest.mark.asyncio
async def test_leaderboard_done_beats_running_for_same_slot(client):
    """When a running job and a done job share (model, dataset), the done row wins."""
    batch_id = "b-mixed"
    await post_event(client, make_batch_start(batch_id, project="p-mixed", n_total=2))
    # Running job (no metrics yet)
    await post_event(
        client,
        make_job_start(batch_id, "j-mx-run", project="p-mixed",
                       model="dlinear", dataset="etth1"),
    )
    # Done job with metrics
    await post_event(
        client,
        make_job_start(batch_id, "j-mx-done", project="p-mixed",
                       model="dlinear", dataset="etth1"),
    )
    await post_event(
        client,
        make_job_done(batch_id, "j-mx-done", project="p-mixed",
                      elapsed_s=30, metrics={"MSE": 0.35}),
    )
    r = await client.get("/api/projects/p-mixed/leaderboard")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "done"
    assert row["best_metric"] == pytest.approx(0.35)
