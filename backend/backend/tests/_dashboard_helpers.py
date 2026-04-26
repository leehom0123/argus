"""Shared helpers for BACKEND-E test suites (dashboard / projects / stars /
pins / compare / health / eta / CSV export).

Keeps each test file focused on assertions; the boilerplate of "post a
batch with these jobs, then mint a second user, then share" lives here.
"""
from __future__ import annotations

import uuid
from typing import Any


async def post_event(client, event: dict, *, headers: dict | None = None) -> None:
    """POST one event with a generated event_id + assert 200."""
    ev = {**event, "event_id": str(uuid.uuid4())}
    kwargs: dict[str, Any] = {"json": ev}
    if headers is not None:
        kwargs["headers"] = headers
    r = await client.post("/api/events", **kwargs)
    assert r.status_code == 200, r.text


async def mk_user_with_token(client, username: str) -> tuple[str, str]:
    """Register + login + mint reporter token. Returns (jwt, api_token)."""
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
    )
    assert lr.status_code == 200
    jwt = lr.json()["access_token"]
    tr = await client.post(
        "/api/tokens",
        json={"name": f"{username}-rep", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert tr.status_code == 201, tr.text
    return jwt, tr.json()["token"]


def make_batch_start(
    batch_id: str,
    project: str = "deepts",
    n_total: int = 2,
    ts: str = "2026-04-23T09:00:00Z",
    user: str | None = None,
    host: str | None = None,
) -> dict:
    source: dict[str, Any] = {"project": project}
    if user is not None:
        source["user"] = user
    if host is not None:
        source["host"] = host
    return {
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": ts,
        "batch_id": batch_id,
        "source": source,
        "data": {"n_total_jobs": n_total, "experiment_type": "forecast"},
    }


def make_job_start(
    batch_id: str,
    job_id: str,
    project: str = "deepts",
    model: str = "transformer",
    dataset: str = "etth1",
    ts: str = "2026-04-23T09:01:00Z",
) -> dict:
    return {
        "schema_version": "1.1",
        "event_type": "job_start",
        "timestamp": ts,
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": project},
        "data": {"model": model, "dataset": dataset},
    }


def make_job_done(
    batch_id: str,
    job_id: str,
    project: str = "deepts",
    elapsed_s: int = 30,
    metrics: dict | None = None,
    ts: str = "2026-04-23T09:02:00Z",
) -> dict:
    if metrics is None:
        metrics = {"MSE": 0.25, "MAE": 0.31, "RMSE": 0.50}
    return {
        "schema_version": "1.1",
        "event_type": "job_done",
        "timestamp": ts,
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": project},
        "data": {
            "status": "DONE",
            "elapsed_s": elapsed_s,
            "train_epochs": 10,
            "metrics": metrics,
        },
    }


def make_job_failed(
    batch_id: str,
    job_id: str,
    project: str = "deepts",
    error: str = "oom",
    ts: str = "2026-04-23T09:02:00Z",
) -> dict:
    return {
        "schema_version": "1.1",
        "event_type": "job_failed",
        "timestamp": ts,
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": project},
        "data": {"error": error, "elapsed_s": 10},
    }


def make_resource_snapshot(
    host: str = "lab-1",
    ts: str = "2026-04-23T09:01:30Z",
    gpu_util: float = 70.0,
    gpu_temp: float = 60.0,
    disk_free_mb: float = 200_000,
    batch_id: str | None = None,
) -> dict:
    src: dict[str, Any] = {"project": "deepts", "host": host}
    ev: dict[str, Any] = {
        "schema_version": "1.1",
        "event_type": "resource_snapshot",
        "timestamp": ts,
        "source": src,
        "data": {
            "gpu_util_pct": gpu_util,
            "gpu_mem_mb": 4000,
            "gpu_mem_total_mb": 24000,
            "gpu_temp_c": gpu_temp,
            "cpu_util_pct": 30,
            "ram_mb": 8000,
            "ram_total_mb": 64000,
            "disk_free_mb": disk_free_mb,
        },
    }
    if batch_id is not None:
        ev["batch_id"] = batch_id
    else:
        # resource_snapshot must have a batch_id per schema; use a
        # throwaway id that still threads the host through.
        ev["batch_id"] = f"res-{uuid.uuid4().hex[:8]}"
    return ev


async def seed_completed_batch(
    client,
    *,
    batch_id: str,
    project: str = "deepts",
    n_total: int = 2,
    metrics: dict | None = None,
    elapsed_s: int = 30,
    headers: dict | None = None,
    model: str = "transformer",
    dataset: str = "etth1",
) -> None:
    """Post a start + 1 job_done so the batch has 1 complete job.

    Useful for leaderboard / export / compare tests.
    """
    await post_event(
        client,
        make_batch_start(
            batch_id,
            project=project,
            n_total=n_total,
            ts="2026-04-23T09:00:00Z",
        ),
        headers=headers,
    )
    job_id = f"{batch_id}-job-0"
    await post_event(
        client,
        make_job_start(
            batch_id,
            job_id,
            project=project,
            model=model,
            dataset=dataset,
            ts="2026-04-23T09:00:30Z",
        ),
        headers=headers,
    )
    await post_event(
        client,
        make_job_done(
            batch_id,
            job_id,
            project=project,
            elapsed_s=elapsed_s,
            metrics=metrics,
            ts="2026-04-23T09:01:00Z",
        ),
        headers=headers,
    )
