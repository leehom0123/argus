"""Resume-after-crash: a second ``batch_start`` with the existing id appends.

When a launcher crashes mid-batch and the user re-runs with the same
``batch_id``, the SDK posts another ``batch_start`` plus fresh
``job_*`` events. The backend must:

* Accept the duplicate ``batch_start`` (no 409) — see
  :func:`backend.api.events._handle_batch_start`.
* Preserve the original ``start_time`` so historical timing isn't reset.
* Append the new ``job_*`` events to the same Batch row, indexable from
  ``GET /api/batches/<id>/events``.
* Leave a previously-``done`` batch alone (idempotent re-run safety).

These tests pin that contract so the v0.2.1 batch-resume feature in
sibyl can rely on it.
"""
from __future__ import annotations

import uuid

import pytest


def _eid() -> str:
    return str(uuid.uuid4())


async def _post(client, ev):
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200, r.text
    return r


def _batch_start(batch_id: str, *, ts: str, n_total: int = 2,
                 command: str | None = None) -> dict:
    data: dict = {"experiment_type": "forecast", "n_total_jobs": n_total}
    if command is not None:
        data["command"] = command
    return {
        "event_id": _eid(),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": ts,
        "batch_id": batch_id,
        "source": {"project": "sibyl"},
        "data": data,
    }


def _job_event(batch_id: str, job_id: str, event_type: str,
               *, ts: str, data: dict | None = None) -> dict:
    return {
        "event_id": _eid(),
        "schema_version": "1.1",
        "event_type": event_type,
        "timestamp": ts,
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": "sibyl"},
        "data": data or {},
    }


@pytest.mark.asyncio
async def test_resume_appends_events_to_existing_batch(client):
    """Two batch_start posts with the same id collapse into one Batch row,
    and events from both phases land under the same id."""
    bid = "bench-resume-001"

    # ── Phase 1: original launch ────────────────────────────────────────
    await _post(client, _batch_start(
        bid, ts="2026-04-25T09:00:00Z", n_total=2, command="python main.py",
    ))
    await _post(client, _job_event(
        bid, "job-A", "job_start", ts="2026-04-25T09:00:30Z",
        data={"model": "m", "dataset": "d"},
    ))
    await _post(client, _job_event(
        bid, "job-A", "job_done", ts="2026-04-25T09:05:00Z",
        data={"status": "DONE", "elapsed_s": 270, "metrics": {"MSE": 0.42}},
    ))

    # ── Crash. ──────────────────────────────────────────────────────────
    # ── Phase 2: resume with the same batch id ──────────────────────────
    await _post(client, _batch_start(
        bid, ts="2026-04-25T10:00:00Z", n_total=2, command="python main.py resume=true",
    ))
    await _post(client, _job_event(
        bid, "job-B", "job_start", ts="2026-04-25T10:00:30Z",
        data={"model": "m", "dataset": "d"},
    ))
    await _post(client, _job_event(
        bid, "job-B", "job_done", ts="2026-04-25T10:05:00Z",
        data={"status": "DONE", "elapsed_s": 270, "metrics": {"MSE": 0.40}},
    ))

    # The Batch row exists, status is back to running (resumed), original
    # start_time preserved (NOT bumped to phase-2 timestamp).
    r = await client.get(f"/api/batches/{bid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == bid
    assert body["status"] == "running"
    assert body["start_time"].startswith("2026-04-25T09:00:00")
    # Both jobs landed on the same batch.
    r = await client.get(f"/api/batches/{bid}/jobs")
    assert r.status_code == 200, r.text
    job_ids = sorted(j["id"] for j in r.json())
    assert job_ids == ["job-A", "job-B"]


@pytest.mark.asyncio
async def test_resume_preserves_finished_batch(client):
    """A re-run hitting a previously-``done`` batch must NOT undo finality.

    Idempotency safety net: if a launcher mistakenly reuses a finished
    batch's id, the backend leaves it ``done`` so the leaderboard /
    notification side effects already triggered don't replay.
    """
    bid = "bench-finished-001"
    await _post(client, _batch_start(bid, ts="2026-04-25T09:00:00Z", n_total=1))
    await _post(client, _job_event(
        bid, "j1", "job_start", ts="2026-04-25T09:00:10Z",
        data={"model": "m", "dataset": "d"},
    ))
    await _post(client, _job_event(
        bid, "j1", "job_done", ts="2026-04-25T09:01:00Z",
        data={"status": "DONE", "elapsed_s": 50, "metrics": {"MSE": 0.5}},
    ))
    # Close the batch.
    await _post(client, {
        "event_id": _eid(),
        "schema_version": "1.1",
        "event_type": "batch_done",
        "timestamp": "2026-04-25T09:02:00Z",
        "batch_id": bid,
        "source": {"project": "sibyl"},
        "data": {"n_done": 1, "n_failed": 0, "total_elapsed_s": 120},
    })
    r = await client.get(f"/api/batches/{bid}")
    assert r.json()["status"] == "done"

    # User accidentally re-runs with the same id.
    await _post(client, _batch_start(bid, ts="2026-04-25T11:00:00Z", n_total=1))

    # Status stays ``done`` — re-init must not undo finality.
    r = await client.get(f"/api/batches/{bid}")
    assert r.json()["status"] == "done"


@pytest.mark.asyncio
async def test_resume_allows_late_arriving_metadata(client):
    """A second batch_start may carry an updated ``command`` / ``n_total``.

    Pure crash recovery rarely changes those, but Optuna trials and
    multi-phase sweeps do — they re-launch with ``-m`` overrides that
    legitimately update both. Idempotent re-init refreshes the metadata
    without forking a new Batch row.
    """
    bid = "bench-meta-001"
    await _post(client, _batch_start(
        bid, ts="2026-04-25T09:00:00Z", n_total=1, command="python main.py",
    ))
    await _post(client, _batch_start(
        bid, ts="2026-04-25T10:00:00Z", n_total=4,
        command="python main.py -m hparams_search=dam_optuna",
    ))
    r = await client.get(f"/api/batches/{bid}")
    body = r.json()
    assert body["n_total"] == 4
    assert "hparams_search=dam_optuna" in body["command"]
    # start_time still anchored at the original launch.
    assert body["start_time"].startswith("2026-04-25T09:00:00")
