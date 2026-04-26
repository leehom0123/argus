"""Tests for the v0.1.3 ProjectSummary density extension.

The density pass on ProjectCard (front-end) needs four new fields on the
project-summary payload that ``GET /api/dashboard`` and ``GET /api/projects``
emit:

* ``failure_rate``    — failed / (done + failed); ``None`` if no jobs ended.
* ``gpu_hours``       — sum of jobs.elapsed_s ÷ 3600 for the project.
* ``top_models``      — up to 3 ``{model, dataset, metric_name, metric_value}``
                       rows ranked by the project's headline metric.
* ``batch_volume_7d`` — list[int] length 7, batch counts per UTC day
                       (oldest → newest).

These tests pin the contract so the front-end can rely on the shape.
"""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import (
    make_batch_start,
    make_job_done,
    make_job_failed,
    make_job_start,
    post_event,
    seed_completed_batch,
)


@pytest.mark.asyncio
async def test_project_summary_includes_density_fields(client):
    """A single completed batch surfaces all four new fields on /api/dashboard."""
    await seed_completed_batch(
        client,
        batch_id="b-density-1",
        project="density-proj",
        metrics={"MSE": 0.42, "MAE": 0.55},
        elapsed_s=3600,  # one full GPU-hour
        model="dlinear",
        dataset="etth1",
    )
    r = await client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    cards = r.json()["projects"]
    card = next(c for c in cards if c["project"] == "density-proj")

    # 1. failure_rate is 0 (one done, no failed).
    assert card["failure_rate"] == 0.0

    # 2. gpu_hours rounds the elapsed_s sum.
    assert card["gpu_hours"] == 1.0

    # 3. top_models has the single completed (model, dataset) row.
    assert isinstance(card["top_models"], list)
    assert len(card["top_models"]) == 1
    top = card["top_models"][0]
    assert top["model"] == "dlinear"
    assert top["dataset"] == "etth1"
    assert top["metric_name"] == "MSE"
    assert top["metric_value"] == pytest.approx(0.42)

    # 4. batch_volume_7d is a length-7 list of ints.
    vol = card["batch_volume_7d"]
    assert isinstance(vol, list)
    assert len(vol) == 7
    assert all(isinstance(v, int) for v in vol)


@pytest.mark.asyncio
async def test_project_summary_failure_rate_with_mixed_results(client):
    """Mixing one job_done and one job_failed → failure_rate around 0.5."""
    await post_event(
        client,
        make_batch_start("b-mixed", project="mixed-proj", n_total=2),
    )
    # One done.
    await post_event(
        client,
        make_job_start(
            "b-mixed", "j-ok", project="mixed-proj",
            model="patchtst", dataset="etth1",
        ),
    )
    await post_event(
        client,
        make_job_done(
            "b-mixed", "j-ok", project="mixed-proj",
            metrics={"MSE": 0.30}, elapsed_s=1800,
        ),
    )
    # One failed.
    await post_event(
        client,
        make_job_start(
            "b-mixed", "j-bad", project="mixed-proj",
            model="patchtst", dataset="etth2",
        ),
    )
    await post_event(
        client,
        make_job_failed("b-mixed", "j-bad", project="mixed-proj"),
    )

    r = await client.get("/api/dashboard")
    cards = r.json()["projects"]
    card = next(c for c in cards if c["project"] == "mixed-proj")

    # n_done bookkeeping treats job_done → 1; job_failed → 1 in n_failed.
    # The sum of done+failed across the *batch* drives failure_rate.
    assert card["failure_rate"] is not None
    assert 0.0 < card["failure_rate"] <= 1.0
    # GPU hours sum over both jobs' elapsed_s (done + failed contribute).
    # 1800s done + 10s failed = 1810s ≈ 0.503h.
    assert card["gpu_hours"] == pytest.approx(0.503, abs=2e-3)


@pytest.mark.asyncio
async def test_project_summary_top_models_picks_best_per_pair(client):
    """Two seeds for the same (model, dataset) collapse to one top row."""
    await post_event(
        client,
        make_batch_start("b-top", project="top-proj", n_total=2),
    )
    await post_event(
        client,
        make_job_start(
            "b-top", "j-seed1", project="top-proj",
            model="transformer", dataset="etth1",
        ),
    )
    await post_event(
        client,
        make_job_done(
            "b-top", "j-seed1", project="top-proj",
            metrics={"MSE": 0.30}, elapsed_s=60,
        ),
    )
    await post_event(
        client,
        make_job_start(
            "b-top", "j-seed2", project="top-proj",
            model="transformer", dataset="etth1",
        ),
    )
    await post_event(
        client,
        make_job_done(
            "b-top", "j-seed2", project="top-proj",
            metrics={"MSE": 0.50}, elapsed_s=60,
        ),
    )

    r = await client.get("/api/dashboard")
    cards = r.json()["projects"]
    card = next(c for c in cards if c["project"] == "top-proj")

    # Only one (transformer, etth1) winner; the better seed wins.
    assert len(card["top_models"]) == 1
    assert card["top_models"][0]["metric_value"] == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_list_projects_includes_density_fields(client):
    """The /api/projects index payload mirrors /api/dashboard density fields."""
    await seed_completed_batch(
        client,
        batch_id="b-list-1",
        project="list-density",
        metrics={"MSE": 0.10, "MAE": 0.20},
        elapsed_s=7200,  # two GPU-hours
        model="itransformer",
        dataset="etth2",
    )
    r = await client.get("/api/projects")
    assert r.status_code == 200, r.text
    rows = r.json()
    row = next(r for r in rows if r["project"] == "list-density")

    assert row["failure_rate"] == 0.0
    assert row["gpu_hours"] == 2.0
    assert isinstance(row["top_models"], list)
    assert len(row["top_models"]) == 1
    assert row["top_models"][0]["model"] == "itransformer"
    assert isinstance(row["batch_volume_7d"], list)
    assert len(row["batch_volume_7d"]) == 7


@pytest.mark.asyncio
async def test_host_summary_includes_running_jobs_top5(client):
    """/api/dashboard host cards now expose the top-5 running jobs list.

    The host-card builder filters resource snapshots to the trailing
    5-minute window, so we have to stamp the snapshot at "now" rather
    than the helper's default fixture timestamp.
    """
    from datetime import datetime, timezone

    from backend.tests._dashboard_helpers import make_resource_snapshot

    await post_event(
        client,
        make_batch_start("b-host-top5", project="host-proj", n_total=1),
    )
    await post_event(
        client,
        make_job_start(
            "b-host-top5", "j-run", project="host-proj",
            model="dlinear", dataset="etth1",
        ),
    )
    # Stamp the snapshot now so the 5-minute host-card cutoff accepts it.
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    await post_event(
        client,
        make_resource_snapshot(
            host="lab-1", batch_id="b-host-top5", ts=now_iso
        ),
    )

    r = await client.get("/api/dashboard")
    body = r.json()
    hosts = body.get("hosts") or []
    assert hosts, "expected at least one host card after resource snapshot"
    card = next(h for h in hosts if h["host"] == "lab-1")
    assert "running_jobs_top5" in card
    assert isinstance(card["running_jobs_top5"], list)
    # Up to five entries; we seeded one running job.
    assert len(card["running_jobs_top5"]) <= 5
