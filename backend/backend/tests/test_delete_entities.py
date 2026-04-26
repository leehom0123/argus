"""Soft-delete endpoints for batches / jobs / projects / hosts (migration 021).

The full matrix exercises:

* DELETE batch as owner / non-owner / admin
* DELETE job as batch owner / non-owner
* DELETE project as admin / non-admin (with cascade verification)
* DELETE host as admin / non-admin
* Soft-deleted rows still in DB after the delete (raw query)

The fixture-provided ``client`` is the first registered user → admin
in the auto-promote-first-user contract; explicit non-admin users go
through ``_register`` which also returns the JWT for header use.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select


def _batch_start_event(batch_id: str, project: str = "proj") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-25T10:00:00Z",
        "batch_id": batch_id,
        "source": {"project": project},
        "data": {"n_total_jobs": 2, "command": "run.py"},
    }


def _job_start_event(batch_id: str, job_id: str, project: str = "proj") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_start",
        "timestamp": "2026-04-25T10:01:00Z",
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": project},
        "data": {"model": "transformer", "dataset": "etth1"},
    }


def _resource_event(batch_id: str, host: str, project: str = "proj") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "resource_snapshot",
        "timestamp": "2026-04-25T10:01:30Z",
        "batch_id": batch_id,
        "source": {"project": project, "host": host},
        "data": {
            "gpu_util_pct": 42,
            "gpu_mem_mb": 1000,
            "gpu_mem_total_mb": 24000,
            "cpu_util_pct": 20,
            "ram_mb": 4000,
            "ram_total_mb": 64000,
        },
    }


def _batch_done_event(batch_id: str, project: str = "proj") -> dict:
    """Move a batch into a terminal status so the v0.1.3 delete guard
    (running/pending/stopping → 409) lets DELETE through.
    """
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_done",
        "timestamp": "2026-04-25T10:05:00Z",
        "batch_id": batch_id,
        "source": {"project": project},
        "data": {"n_done": 1, "n_failed": 0, "total_elapsed_s": 30},
    }


def _job_done_event(
    batch_id: str, job_id: str, project: str = "proj"
) -> dict:
    """Move a job into a ``done`` status so the v0.1.3 delete guard
    (running/pending → 409) lets DELETE /api/jobs through.
    """
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_done",
        "timestamp": "2026-04-25T10:04:00Z",
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": project},
        "data": {
            "status": "DONE",
            "elapsed_s": 30,
            "train_epochs": 10,
            "metrics": {"MSE": 0.25},
        },
    }


async def _register(client, username: str) -> tuple[str, str]:
    """Register + login a user; return ``(jwt, api_token)``.

    The default ``client`` fixture's ``tester`` user is first → admin,
    so any user registered through this helper is a regular non-admin.
    """
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
    jwt = lr.json()["access_token"]
    tr = await client.post(
        "/api/tokens",
        json={"name": f"{username}-rep", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    return jwt, tr.json()["token"]


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_batch_as_owner(client):
    """Owner deletes the batch; subsequent GET returns 404."""
    r = await client.post("/api/events", json=_batch_start_event("del-1"))
    assert r.status_code == 200
    # v0.1.3 delete guard requires terminal status before soft-delete.
    await client.post("/api/events", json=_batch_done_event("del-1"))

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.delete(
        "/api/batches/del-1",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "deleted"

    # GET now 404
    r = await client.get(
        "/api/batches/del-1",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_batch_as_non_owner_403(client):
    """Non-owner / non-admin hitting DELETE → 403."""
    r = await client.post("/api/events", json=_batch_start_event("del-2"))
    assert r.status_code == 200

    alice_jwt, alice_tok = await _register(client, "alice")

    r = await client.delete(
        "/api/batches/del-2",
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_delete_batch_idempotent_returns_404(client):
    """Re-deleting a soft-deleted batch returns 404 (the caller can't see it)."""
    r = await client.post("/api/events", json=_batch_start_event("del-3"))
    assert r.status_code == 200
    await client.post("/api/events", json=_batch_done_event("del-3"))

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.delete(
        "/api/batches/del-3",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200

    r2 = await client.delete(
        "/api/batches/del-3",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_job_as_batch_owner(client):
    """Batch owner can soft-delete a single job."""
    await client.post("/api/events", json=_batch_start_event("del-job-1"))
    await client.post(
        "/api/events", json=_job_start_event("del-job-1", "j-a")
    )
    # v0.1.3 delete guard requires job in terminal status.
    await client.post("/api/events", json=_job_done_event("del-job-1", "j-a"))

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.delete(
        "/api/jobs/del-job-1/j-a",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text

    # GET job → 404
    r = await client.get(
        "/api/jobs/del-job-1/j-a",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 404

    # The job no longer appears in the per-batch jobs list either.
    r = await client.get(
        "/api/batches/del-job-1/jobs",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200
    job_ids = [j["id"] for j in r.json()]
    assert "j-a" not in job_ids


@pytest.mark.asyncio
async def test_delete_job_as_non_owner_403(client):
    """Non-owner attempting to delete a job receives 403."""
    await client.post("/api/events", json=_batch_start_event("del-job-2"))
    await client.post(
        "/api/events", json=_job_start_event("del-job-2", "j-b")
    )

    alice_jwt, _ = await _register(client, "alice2")

    r = await client.delete(
        "/api/jobs/del-job-2/j-b",
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_project_as_admin_cascades_to_batches(client):
    """Admin deletes a project; every batch under it disappears too."""
    # Two batches share the same project name.
    await client.post(
        "/api/events", json=_batch_start_event("p-batch-1", project="alpha")
    )
    await client.post(
        "/api/events", json=_batch_start_event("p-batch-2", project="alpha")
    )

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.delete(
        "/api/projects/alpha",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text

    # GET project → 404
    r = await client.get(
        "/api/projects/alpha",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 404

    # Both batches now invisible.
    for bid in ("p-batch-1", "p-batch-2"):
        r = await client.get(
            f"/api/batches/{bid}",
            headers={"Authorization": f"Bearer {tester_jwt}"},
        )
        assert r.status_code == 404

    # The project no longer appears in the project list.
    r = await client.get(
        "/api/projects",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200
    project_names = [p["project"] for p in r.json()]
    assert "alpha" not in project_names


@pytest.mark.asyncio
async def test_delete_project_as_non_admin_403(client):
    """Non-admin attempting to delete a project receives 403."""
    await client.post(
        "/api/events", json=_batch_start_event("p-batch-3", project="bravo")
    )

    bob_jwt, _ = await _register(client, "bob_proj")

    r = await client.delete(
        "/api/projects/bravo",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_host_as_admin(client):
    """Admin deletes a host; subsequent list-hosts hides it."""
    # Seed a host snapshot via a batch.
    await client.post(
        "/api/events", json=_batch_start_event("h-batch-1", project="proj")
    )
    await client.post(
        "/api/events", json=_resource_event("h-batch-1", host="gpu-node-7")
    )

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Pre-condition: the host appears in the listing.
    r = await client.get(
        "/api/resources/hosts",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200
    assert "gpu-node-7" in r.json()

    r = await client.delete(
        "/api/hosts/gpu-node-7",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text

    # Post-condition: hidden from the list.
    r = await client.get(
        "/api/resources/hosts",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert "gpu-node-7" not in r.json()

    # And the timeseries surface returns 404.
    r = await client.get(
        "/api/hosts/gpu-node-7/timeseries",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_host_as_non_admin_403(client):
    """Non-admin → 403 on host deletion."""
    bob_jwt, _ = await _register(client, "bob_host")

    r = await client.delete(
        "/api/hosts/some-host",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# DB-level: soft-deleted records remain in the table
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_delete_batches_all_owned(client):
    """Bulk-delete five owned batches → all five end up in ``deleted``."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    ids = [f"bulk-b-{i}" for i in range(5)]
    for bid in ids:
        await client.post("/api/events", json=_batch_start_event(bid))
        # v0.1.3 delete guard requires terminal status.
        await client.post("/api/events", json=_batch_done_event(bid))

    r = await client.post(
        "/api/batches/bulk-delete",
        json={"batch_ids": ids},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == sorted(ids)
    assert body["skipped"] == []


@pytest.mark.asyncio
async def test_bulk_delete_batches_mixed_ownership(client):
    """3 owned + 2 not-owned → 3 deleted, 2 skipped with reason ``not_owner``."""
    # Tester creates 3 batches and terminates them so the delete
    # guard (running → 409) doesn't override the not_owner reason
    # under test.
    owned = [f"mix-mine-{i}" for i in range(3)]
    for bid in owned:
        await client.post("/api/events", json=_batch_start_event(bid))
        await client.post("/api/events", json=_batch_done_event(bid))

    # Alice creates 2 batches — non-admin — and terminates them too.
    alice_jwt, alice_tok = await _register(client, "alice_bulk")
    others = [f"mix-alice-{i}" for i in range(2)]
    for bid in others:
        ev = _batch_start_event(bid)
        await client.post(
            "/api/events",
            json=ev,
            headers={"Authorization": f"Bearer {alice_tok}"},
        )
        await client.post(
            "/api/events",
            json=_batch_done_event(bid),
            headers={"Authorization": f"Bearer {alice_tok}"},
        )

    # Alice attempts to delete all 5 — only her 2 succeed, the 3 owned
    # by tester are skipped.
    r = await client.post(
        "/api/batches/bulk-delete",
        json={"batch_ids": owned + others},
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == sorted(others)
    skip_ids = sorted(s["id"] for s in body["skipped"])
    assert skip_ids == sorted(owned)
    for s in body["skipped"]:
        assert s["reason"] == "not_owner"


@pytest.mark.asyncio
async def test_bulk_delete_batches_already_deleted_idempotent(client):
    """Including an already-deleted id yields ``reason=already_deleted``."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    bid = "bulk-already-1"
    await client.post("/api/events", json=_batch_start_event(bid))
    # v0.1.3 guard: terminate before delete.
    await client.post("/api/events", json=_batch_done_event(bid))
    # First delete
    assert (
        await client.delete(
            f"/api/batches/{bid}",
            headers={"Authorization": f"Bearer {tester_jwt}"},
        )
    ).status_code == 200

    r = await client.post(
        "/api/batches/bulk-delete",
        json={"batch_ids": [bid]},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == []
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["id"] == bid
    assert body["skipped"][0]["reason"] == "already_deleted"


@pytest.mark.asyncio
async def test_bulk_delete_batches_empty_list_400(client):
    """Empty payload is rejected with 400."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/batches/bulk-delete",
        json={"batch_ids": []},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_bulk_delete_jobs_partial(client):
    """Bulk-delete jobs returns deleted + skipped per item."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post("/api/events", json=_batch_start_event("bd-jobs"))
    for jid in ("ja", "jb", "jc"):
        await client.post(
            "/api/events",
            json=_job_start_event("bd-jobs", jid),
        )
        # v0.1.3 guard: jobs must be terminal to be deletable.
        await client.post(
            "/api/events", json=_job_done_event("bd-jobs", jid)
        )

    items = [
        {"batch_id": "bd-jobs", "job_id": "ja"},
        {"batch_id": "bd-jobs", "job_id": "missing"},
        {"batch_id": "bd-jobs", "job_id": "jb"},
    ]
    r = await client.post(
        "/api/jobs/bulk-delete",
        json={"items": items},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == ["bd-jobs/ja", "bd-jobs/jb"]
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["id"] == "bd-jobs/missing"
    assert body["skipped"][0]["reason"] == "not_found"


@pytest.mark.asyncio
async def test_bulk_delete_projects_admin_cascades(client):
    """Admin bulk-delete cascades to batches under each project."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post(
        "/api/events", json=_batch_start_event("bd-p1", project="alpha2")
    )
    await client.post(
        "/api/events", json=_batch_start_event("bd-p2", project="beta2")
    )

    r = await client.post(
        "/api/admin/projects/bulk-delete",
        json={"projects": ["alpha2", "beta2", "missing-proj"]},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == ["alpha2", "beta2"]
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["id"] == "missing-proj"
    assert body["skipped"][0]["reason"] == "not_found"

    # Both batches should now be invisible.
    for bid in ("bd-p1", "bd-p2"):
        r = await client.get(
            f"/api/batches/{bid}",
            headers={"Authorization": f"Bearer {tester_jwt}"},
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_bulk_delete_hosts_admin(client):
    """Admin bulk-delete hides multiple hosts."""
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post("/api/events", json=_batch_start_event("bd-host"))
    for h in ("hA", "hB"):
        await client.post(
            "/api/events", json=_resource_event("bd-host", host=h)
        )

    r = await client.post(
        "/api/admin/hosts/bulk-delete",
        json={"hosts": ["hA", "hB"]},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == ["hA", "hB"]

    r = await client.get(
        "/api/resources/hosts",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    listed = r.json()
    assert "hA" not in listed
    assert "hB" not in listed


@pytest.mark.asyncio
async def test_bulk_delete_projects_non_admin_403(client):
    """Non-admin hitting the admin bulk-delete endpoint gets 403."""
    bob_jwt, _ = await _register(client, "bob_bulk")
    r = await client.post(
        "/api/admin/projects/bulk-delete",
        json={"projects": ["whatever"]},
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_soft_deleted_records_still_in_db(client):
    """Confirm the rows aren't hard-deleted — only flagged."""
    # Seed batch + job + host snapshot under project "gamma".
    await client.post(
        "/api/events", json=_batch_start_event("raw-1", project="gamma")
    )
    await client.post(
        "/api/events", json=_job_start_event("raw-1", "j-raw", project="gamma")
    )
    # v0.1.3 guard: terminate the job before delete.
    await client.post(
        "/api/events",
        json=_job_done_event("raw-1", "j-raw", project="gamma"),
    )
    await client.post(
        "/api/events", json=_resource_event("raw-1", host="raw-host-1", project="gamma")
    )

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Delete each entity. Order chosen so each delete still finds its
    # target (project delete cascades to the batch, so we run project
    # last among the batch/project pair).
    assert (await client.delete(
        "/api/jobs/raw-1/j-raw",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )).status_code == 200
    # Project delete (admin only — tester is admin) cascades to the batch.
    assert (await client.delete(
        "/api/projects/gamma",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )).status_code == 200
    assert (await client.delete(
        "/api/hosts/raw-host-1",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )).status_code == 200

    # Raw DB query — every row should still exist with is_deleted=True.
    import backend.db as db_mod
    from backend.models import Batch, HostMeta, Job, ProjectMeta

    async with db_mod.SessionLocal() as session:
        b = await session.get(Batch, "raw-1")
        assert b is not None
        assert b.is_deleted is True

        j = await session.get(Job, ("j-raw", "raw-1"))
        assert j is not None
        assert j.is_deleted is True

        pm = await session.get(ProjectMeta, "gamma")
        assert pm is not None
        assert pm.is_deleted is True

        hm = await session.get(HostMeta, "raw-host-1")
        assert hm is not None
        assert hm.is_deleted is True
