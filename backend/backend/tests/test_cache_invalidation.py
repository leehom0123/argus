"""Cache invalidation regression tests for stop / delete / share mutations.

The TTL response cache (``backend.utils.response_cache.default_cache``)
gives every read endpoint a 10-second window. Without explicit
invalidation, a user can stop a batch / delete a job / receive a project
share and still see the stale "running" / "alive" / "no shared project"
payload until the TTL expires.

These tests cover three contracts:

1. **stop_batch** — after ``POST /api/batches/{id}/stop``, the next
   ``GET /api/batches/{id}`` reflects ``status='stopping'`` instead of
   the cached pre-stop body.
2. **delete_batch** — after ``DELETE /api/batches/{id}``, the next
   ``GET /api/batches/{id}`` returns 404 instead of the cached 200 body.
3. **delete_job** — after ``DELETE /api/jobs/{batch}/{job}``, the next
   ``GET /api/batches/{batch}/jobs`` no longer lists the deleted job.
4. **add_project_share** — after a project share is granted to bob,
   bob's next ``GET /api/projects`` includes the new project (instead
   of the cached empty list).
5. **remove_project_share** — after revocation, bob's next list drops it.
"""
from __future__ import annotations

import uuid

import pytest


async def _seed_batch_with_job(
    client,
    batch_id: str,
    job_id: str = "j-1",
    project: str = "proj-cache-inv",
    *,
    terminate: bool = False,
) -> None:
    """Post a batch_start + job_start so the batch has visible state.

    Set ``terminate=True`` to additionally fire a ``job_done`` and
    ``batch_done`` so the batch lands in a terminal status — required
    by the v0.1.3 delete guards before ``DELETE`` returns 200.
    """
    src = {"project": project, "user": "tester"}
    await client.post("/api/events", json={
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-25T13:00:00Z",
        "batch_id": batch_id,
        "source": src,
        "data": {"n_total_jobs": 2},
    })
    await client.post("/api/events", json={
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_start",
        "timestamp": "2026-04-25T13:01:00Z",
        "batch_id": batch_id,
        "job_id": job_id,
        "source": src,
        "data": {"model": "transformer", "dataset": "etth1"},
    })
    if terminate:
        await client.post("/api/events", json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_done",
            "timestamp": "2026-04-25T13:02:00Z",
            "batch_id": batch_id,
            "job_id": job_id,
            "source": src,
            "data": {
                "status": "DONE",
                "elapsed_s": 60,
                "train_epochs": 10,
                "metrics": {"MSE": 0.25},
            },
        })
        await client.post("/api/events", json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "batch_done",
            "timestamp": "2026-04-25T13:03:00Z",
            "batch_id": batch_id,
            "source": src,
            "data": {"n_done": 1, "n_failed": 0, "total_elapsed_s": 60},
        })


async def _mk_user(client, username: str) -> tuple[str, str]:
    """Register + login a non-admin user; return ``(jwt, api_token)``."""
    reg = await client.post("/api/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": "password123",
    })
    assert reg.status_code == 201, reg.text
    lr = await client.post("/api/auth/login", json={
        "username_or_email": username,
        "password": "password123",
    })
    jwt = lr.json()["access_token"]
    tr = await client.post(
        "/api/tokens",
        json={"name": f"{username}-rep", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    return jwt, tr.json()["token"]


# ---------------------------------------------------------------------------
# 1. stop_batch — cached "running" payload must not survive the stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_batch_invalidates_batch_cache(client):
    """Cached batch detail (status='running') is dropped after POST /stop.

    Pre-fix the GET handler kept the 'running' payload for up to 10s
    after the stop POST committed; the cache invalidation hook in
    ``stop_batch`` ensures the next GET reflects the new status
    immediately.
    """
    await _seed_batch_with_job(client, "b-stop-cache-1")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Warm the cache with a GET — payload reflects the running state.
    r1 = await client.get(
        "/api/batches/b-stop-cache-1",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r1.status_code == 200, r1.text
    pre_status = r1.json()["status"]
    # batch_start sets status='running' on first ingest; if the seed
    # ever changes the convention, this assertion catches it.
    assert pre_status == "running", pre_status

    # Stop it.
    rs = await client.post(
        "/api/batches/b-stop-cache-1/stop",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert rs.status_code == 200, rs.text
    assert rs.json()["status"] == "stopping"

    # Immediate re-read must reflect the new status, not the cached one.
    r2 = await client.get(
        "/api/batches/b-stop-cache-1",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "stopping", (
        "GET /batches/{id} returned the cached 'running' payload after a "
        "successful POST /stop — invalidation is missing"
    )


# ---------------------------------------------------------------------------
# 2. delete_batch — cached 200 must turn into a fresh 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_batch_invalidates_batch_cache(client):
    """Cached batch detail body is dropped after DELETE /batches/{id}."""
    # v0.1.3 delete guard rejects active batches; terminate first.
    await _seed_batch_with_job(client, "b-del-cache-1", terminate=True)
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Warm the cache.
    r1 = await client.get(
        "/api/batches/b-del-cache-1",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r1.status_code == 200

    # Delete (soft).
    rd = await client.delete(
        "/api/batches/b-del-cache-1",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert rd.status_code == 200

    # The handler raises 404 when ``is_deleted=True``; if cache invalidation
    # is missing, the GET will return the stale cached 200 body instead.
    r2 = await client.get(
        "/api/batches/b-del-cache-1",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r2.status_code == 404, (
        f"GET /batches/{{id}} returned {r2.status_code} after DELETE — "
        f"cached 200 body leaked: {r2.text}"
    )

    # The batches-list cache must also drop the deleted id.
    rl = await client.get(
        "/api/batches",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    ids = {b["id"] for b in rl.json()}
    assert "b-del-cache-1" not in ids, (
        f"batches-list still shows deleted batch: {ids}"
    )


# ---------------------------------------------------------------------------
# 3. delete_job — cached batch-jobs list must drop the deleted job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_job_invalidates_batch_jobs_cache(client):
    """Cached /batches/{id}/jobs list drops the deleted job after DELETE."""
    # v0.1.3 delete guard rejects running jobs; terminate first.
    await _seed_batch_with_job(
        client, "b-del-job-cache-1", job_id="j-to-delete", terminate=True
    )
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Warm the per-batch jobs cache.
    r1 = await client.get(
        "/api/batches/b-del-job-cache-1/jobs",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r1.status_code == 200
    ids = {j["id"] for j in r1.json()}
    assert "j-to-delete" in ids

    # Delete the job (soft).
    rd = await client.delete(
        "/api/jobs/b-del-job-cache-1/j-to-delete",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert rd.status_code == 200, rd.text

    # The cached body still contains j-to-delete; verify the cache was
    # dropped and the next GET reflects reality.
    r2 = await client.get(
        "/api/batches/b-del-job-cache-1/jobs",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r2.status_code == 200
    ids2 = {j["id"] for j in r2.json()}
    assert "j-to-delete" not in ids2, (
        f"GET /batches/{{id}}/jobs still lists deleted job: {ids2}"
    )


# ---------------------------------------------------------------------------
# 4. add_project_share — grantee's projects-list cache must be busted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_share_grant_invalidates_grantee_projects_cache(client):
    """A freshly-shared project surfaces immediately on bob's GET /projects."""
    # Tester (admin) seeds a project + batch.
    await _seed_batch_with_job(
        client, "b-share-cache-1", project="freshly-shared"
    )
    bob_jwt, _ = await _mk_user(client, "bob")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Bob's pre-share view: empty list. This warms his projects-list cache.
    r0 = await client.get(
        "/api/projects",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r0.status_code == 200
    pre_names = {p["project"] for p in r0.json()}
    assert "freshly-shared" not in pre_names

    # Tester grants the share.
    rs = await client.post(
        "/api/projects/shares",
        json={
            "project": "freshly-shared",
            "grantee_username": "bob",
            "permission": "viewer",
        },
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert rs.status_code == 201, rs.text

    # Bob's next list must include the new project, not return the
    # cached empty list.
    r1 = await client.get(
        "/api/projects",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r1.status_code == 200
    names = {p["project"] for p in r1.json()}
    assert "freshly-shared" in names, (
        f"bob's GET /projects returned cached empty list after share grant: "
        f"{names}"
    )


@pytest.mark.asyncio
async def test_project_share_grant_invalidates_grantee_dashboard_cache(client):
    """The grantee's cached dashboard payload drops after a project share."""
    await _seed_batch_with_job(
        client, "b-share-dash-1", project="dash-shared"
    )
    bob_jwt, _ = await _mk_user(client, "bob")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Warm bob's dashboard cache while he can't yet see the project.
    r0 = await client.get(
        "/api/dashboard",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r0.status_code == 200
    pre = {p["project"] for p in r0.json()["projects"]}
    assert "dash-shared" not in pre

    # Grant.
    rs = await client.post(
        "/api/projects/shares",
        json={
            "project": "dash-shared",
            "grantee_username": "bob",
            "permission": "viewer",
        },
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert rs.status_code == 201

    # Dashboard must reflect the new visibility.
    r1 = await client.get(
        "/api/dashboard",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r1.status_code == 200
    post = {p["project"] for p in r1.json()["projects"]}
    assert "dash-shared" in post, (
        f"bob's dashboard returned cached payload after share grant: {post}"
    )


# ---------------------------------------------------------------------------
# 5. remove_project_share — revoked project must drop from grantee's list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_share_revoke_invalidates_grantee_projects_cache(client):
    """Revoking a share drops the project from the grantee's next GET."""
    await _seed_batch_with_job(
        client, "b-share-rev-1", project="will-be-revoked"
    )
    bob_jwt, _ = await _mk_user(client, "bob")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Grant.
    rs = await client.post(
        "/api/projects/shares",
        json={
            "project": "will-be-revoked",
            "grantee_username": "bob",
            "permission": "viewer",
        },
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert rs.status_code == 201, rs.text
    grantee_id = rs.json()["grantee_id"]

    # Bob warms his cache while the share is active.
    r0 = await client.get(
        "/api/projects",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r0.status_code == 200
    names = {p["project"] for p in r0.json()}
    assert "will-be-revoked" in names

    # Revoke.
    rr = await client.delete(
        f"/api/projects/shares/will-be-revoked/{grantee_id}",
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert rr.status_code == 200

    # Bob's next read must drop the project.
    r1 = await client.get(
        "/api/projects",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r1.status_code == 200
    names2 = {p["project"] for p in r1.json()}
    assert "will-be-revoked" not in names2, (
        f"bob's GET /projects still shows revoked project: {names2}"
    )
