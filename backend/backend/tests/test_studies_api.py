"""Tests for ``/api/studies`` — Optuna trial visualisation (v0.2 hyperopt-ui).

The endpoints derive everything from ``Job.extra.optuna`` (set by Sibyl's
``Monitor`` callback on ``job_start``) so the suite seeds plain
``job_start`` events with the optuna block in ``data`` and asserts the
aggregations come back correctly. RBAC piggybacks on the shared
:class:`VisibilityResolver` — covered by the cross-user test below.
"""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import (
    make_batch_start,
    make_job_done,
    make_job_start,
    mk_user_with_token,
    post_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _job_start_with_optuna(
    batch_id: str,
    job_id: str,
    *,
    study_name: str,
    trial_id: int,
    params: dict,
    direction: str = "minimize",
    sampler: str = "TPESampler",
    target_metric: str | None = None,
    project: str = "deepts",
    model: str = "transformer",
    dataset: str = "etth1",
    ts: str = "2026-04-23T09:01:00Z",
) -> dict:
    """Build a ``job_start`` event with the ``optuna`` block in ``data``.

    Mirrors what Sibyl's monitor.py emits when running under
    ``hydra/sweeper: optuna``. The events handler stashes everything
    except ``model``/``dataset`` on ``Job.extra`` (see
    ``api/events.py::_handle_job_start``).
    """
    optuna_block: dict = {
        "study_name": study_name,
        "trial_id": trial_id,
        "params": params,
        "direction": direction,
        "sampler": sampler,
    }
    if target_metric is not None:
        optuna_block["target_metric"] = target_metric
    return {
        "schema_version": "1.1",
        "event_type": "job_start",
        "timestamp": ts,
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": project},
        "data": {"model": model, "dataset": dataset, "optuna": optuna_block},
    }


async def _seed_trial(
    client,
    *,
    batch_id: str,
    job_id: str,
    study_name: str,
    trial_id: int,
    params: dict,
    metrics: dict | None = None,
    direction: str = "minimize",
    target_metric: str | None = None,
    headers: dict | None = None,
    ts_start: str = "2026-04-23T09:01:00Z",
    ts_done: str = "2026-04-23T09:02:00Z",
) -> None:
    """Post the 3-event sequence (batch_start → job_start → job_done)."""
    await post_event(
        client, make_batch_start(batch_id), headers=headers,
    )
    await post_event(
        client,
        _job_start_with_optuna(
            batch_id,
            job_id,
            study_name=study_name,
            trial_id=trial_id,
            params=params,
            direction=direction,
            target_metric=target_metric,
            ts=ts_start,
        ),
        headers=headers,
    )
    if metrics is not None:
        await post_event(
            client,
            make_job_done(batch_id, job_id, metrics=metrics, ts=ts_done),
            headers=headers,
        )


# ---------------------------------------------------------------------------
# GET /api/studies
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_studies_aggregates_trials(client):
    """Three trials of one study collapse into a single summary row."""
    for i, (mse, lr) in enumerate([(0.5, 1e-3), (0.2, 5e-4), (0.7, 2e-3)]):
        await _seed_trial(
            client,
            batch_id=f"batch-{i}",
            job_id=f"trial-{i}",
            study_name="dam_forecast_optimization",
            trial_id=i,
            params={"optimizer.lr": lr, "model.d_model": 128},
            metrics={"MSE": mse, "MAE": mse * 1.2},
        )

    r = await client.get("/api/studies")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "studies" in body
    assert len(body["studies"]) == 1
    s = body["studies"][0]
    assert s["study_name"] == "dam_forecast_optimization"
    assert s["n_trials"] == 3
    assert s["n_done"] == 3
    assert s["n_failed"] == 0
    # minimize → 0.2 wins
    assert s["best_value"] == pytest.approx(0.2)
    assert s["best_metric"] == "MSE"
    assert s["direction"] == "minimize"
    assert s["sampler"] == "TPESampler"


@pytest.mark.asyncio
async def test_list_studies_separates_distinct_names(client):
    """Two different study_names → two rows in the list."""
    await _seed_trial(
        client,
        batch_id="b-a",
        job_id="t-a",
        study_name="study_alpha",
        trial_id=0,
        params={"lr": 1e-3},
        metrics={"MSE": 0.3},
    )
    await _seed_trial(
        client,
        batch_id="b-b",
        job_id="t-b",
        study_name="study_beta",
        trial_id=0,
        params={"lr": 1e-4},
        metrics={"MSE": 0.4},
    )
    r = await client.get("/api/studies")
    assert r.status_code == 200
    names = {s["study_name"] for s in r.json()["studies"]}
    assert names == {"study_alpha", "study_beta"}


@pytest.mark.asyncio
async def test_list_studies_ignores_non_optuna_jobs(client):
    """Plain (non-Optuna) jobs do not appear in the studies list."""
    # Plain job — no optuna block.
    await post_event(client, make_batch_start("plain"))
    await post_event(client, make_job_start("plain", "j-plain"))
    await post_event(client, make_job_done("plain", "j-plain"))

    # Optuna job.
    await _seed_trial(
        client,
        batch_id="opt",
        job_id="t-0",
        study_name="study_x",
        trial_id=0,
        params={"lr": 1e-3},
        metrics={"MSE": 0.1},
    )

    r = await client.get("/api/studies")
    assert r.status_code == 200
    names = [s["study_name"] for s in r.json()["studies"]]
    assert names == ["study_x"]


# ---------------------------------------------------------------------------
# GET /api/studies/{name}
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_study_returns_trials_with_params(client):
    """Trial detail view exposes params + value + sortable fields."""
    for i, mse in enumerate([0.7, 0.3, 0.5]):
        await _seed_trial(
            client,
            batch_id=f"b-{i}",
            job_id=f"t-{i}",
            study_name="study_q",
            trial_id=i,
            params={"optimizer.lr": 0.001 * (i + 1), "model.d_model": 64 << i},
            metrics={"MSE": mse},
        )

    r = await client.get("/api/studies/study_q")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["study_name"] == "study_q"
    assert body["n_trials"] == 3
    assert body["n_done"] == 3
    # Default sort: value asc → minimize → best (0.3) first.
    values = [t["value"] for t in body["trials"]]
    assert values == [0.3, 0.5, 0.7]
    assert {"optimizer.lr", "model.d_model"} <= set(body["param_keys"])
    assert "MSE" in body["metric_keys"]
    # Sanity: the best trial's params survived the round-trip.
    best = body["trials"][0]
    assert best["params"]["model.d_model"] == 128
    assert best["trial_id"] == 1
    # ``job_id``+``batch_id`` link back to the underlying job.
    assert best["job_id"] == "t-1"
    assert best["batch_id"] == "b-1"


@pytest.mark.asyncio
async def test_get_study_sort_by_trial_id_desc(client):
    """``sort=trial_id&order=desc`` sorts numerically, not lexicographically."""
    for i in range(12):
        await _seed_trial(
            client,
            batch_id=f"b-{i}",
            job_id=f"t-{i}",
            study_name="study_z",
            trial_id=i,
            params={"x": i},
            metrics={"MSE": float(i)},
        )

    r = await client.get("/api/studies/study_z?sort=trial_id&order=desc")
    assert r.status_code == 200
    ids = [t["trial_id"] for t in r.json()["trials"]]
    # Numeric sort: 11,10,9,...,0 — would be 9,8,7,...,11,10 if string-sorted.
    assert ids == list(range(11, -1, -1))


@pytest.mark.asyncio
async def test_get_study_unknown_name_returns_empty_not_404(client):
    """A study name nobody has reported yet returns an empty trial list."""
    r = await client.get("/api/studies/never_seen")
    assert r.status_code == 200
    body = r.json()
    assert body["study_name"] == "never_seen"
    assert body["n_trials"] == 0
    assert body["trials"] == []


@pytest.mark.asyncio
async def test_get_study_rejects_invalid_sort(client):
    """Arbitrary sort fields → 400 with the i18n key."""
    r = await client.get("/api/studies/anything?sort=injected")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/studies/{name}/trials/{id}
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_trial_detail_roundtrip(client):
    """Single-trial detail returns the full param + metric blob."""
    await _seed_trial(
        client,
        batch_id="bb",
        job_id="tt",
        study_name="study_one",
        trial_id=7,
        params={"optimizer.lr": 5e-4, "model.dropout": 0.1},
        metrics={"MSE": 0.18, "MAE": 0.22},
        target_metric="MSE",
    )
    r = await client.get("/api/studies/study_one/trials/7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trial_id"] == 7
    assert body["job_id"] == "tt"
    assert body["batch_id"] == "bb"
    assert body["params"]["optimizer.lr"] == pytest.approx(5e-4)
    assert body["value"] == pytest.approx(0.18)
    assert body["metric_name"] == "MSE"


@pytest.mark.asyncio
async def test_get_trial_404_when_missing(client):
    """Unknown trial id → 404."""
    await _seed_trial(
        client,
        batch_id="b1",
        job_id="t1",
        study_name="study_present",
        trial_id=0,
        params={"lr": 1e-3},
        metrics={"MSE": 0.1},
    )
    r = await client.get("/api/studies/study_present/trials/999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# RBAC — visibility filtering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_studies_respect_visibility(client):
    """Bob cannot see Alice's optuna trials.

    Alice (the default ``client`` user — first user, admin) seeds a
    study. Bob registers separately and gets back an empty list,
    proving the visibility filter is wired through. A direct GET on
    the study name returns an empty trial list (not 404, because
    "no visible trials" is a legitimate state).
    """
    # Default client is the first registered user → admin. Seed with
    # an explicit non-admin user instead so the share boundary is
    # meaningful.
    alice_jwt, alice_token = await mk_user_with_token(client, "alice")
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    await _seed_trial(
        client,
        batch_id="alice-batch",
        job_id="alice-trial",
        study_name="alice_only",
        trial_id=0,
        params={"lr": 1e-3},
        metrics={"MSE": 0.2},
        headers=alice_headers,
    )

    bob_jwt, _ = await mk_user_with_token(client, "bob")
    bob_jwt_headers = {"Authorization": f"Bearer {bob_jwt}"}

    # Bob's list is empty.
    r = await client.get("/api/studies", headers=bob_jwt_headers)
    assert r.status_code == 200
    assert r.json()["studies"] == []

    # Bob's detail call returns the empty-shape (visibility-filtered).
    r = await client.get(
        "/api/studies/alice_only", headers=bob_jwt_headers,
    )
    assert r.status_code == 200
    assert r.json()["trials"] == []

    # And Bob can't reach the trial detail directly either.
    r = await client.get(
        "/api/studies/alice_only/trials/0", headers=bob_jwt_headers,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_studies_require_auth(unauthed_client):
    """Without a token every endpoint is 401."""
    r = await unauthed_client.get("/api/studies")
    assert r.status_code == 401
    r = await unauthed_client.get("/api/studies/x")
    assert r.status_code == 401
    r = await unauthed_client.get("/api/studies/x/trials/0")
    assert r.status_code == 401
