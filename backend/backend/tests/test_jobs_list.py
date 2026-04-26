"""Tests for the global ``GET /api/jobs`` endpoint (#118).

Exercises pagination, filter combos, visibility/RBAC inheritance from
``VisibilityResolver``, and the ``since`` shorthand parser.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


async def _post(client, ev, headers=None):
    """POST one event with a generated ``event_id`` (v1.1 contract)."""
    ev = {**ev, "event_id": str(uuid.uuid4())}
    r = await client.post("/api/events", json=ev, headers=headers)
    assert r.status_code == 200, r.text


async def _seed_batch_with_jobs(
    client,
    *,
    batch_id: str,
    project: str = "p1",
    host: str = "h1",
    user: str = "tester",
    n_jobs: int = 1,
    statuses: list[str] | None = None,
    job_start: datetime | None = None,
    headers=None,
) -> None:
    """Seed one batch + N jobs via ``POST /api/events``.

    ``statuses`` lets each job carry a distinct status (``done`` / ``failed`` /
    ``running``); when omitted every job is created in the default
    ``running`` state from the ``job_start`` event.
    """
    base_ts = (job_start or _utcnow()).isoformat().replace("+00:00", "Z")
    # 1) batch_start
    await _post(
        client,
        {
            "schema_version": "1.1",
            "event_type": "batch_start",
            "timestamp": base_ts,
            "batch_id": batch_id,
            "source": {"project": project, "user": user, "host": host},
            "data": {"n_total_jobs": n_jobs},
        },
        headers=headers,
    )
    # 2) per-job: job_start + (optional) terminal status event
    for i in range(n_jobs):
        job_id = f"{batch_id}-j{i}"
        st = (statuses or ["running"] * n_jobs)[i]
        await _post(
            client,
            {
                "schema_version": "1.1",
                "event_type": "job_start",
                "timestamp": base_ts,
                "batch_id": batch_id,
                "job_id": job_id,
                "source": {"project": project, "user": user, "host": host},
                "data": {"model": "transformer", "dataset": "etth1"},
            },
            headers=headers,
        )
        if st in ("done", "failed"):
            ev_type = "job_done" if st == "done" else "job_failed"
            await _post(
                client,
                {
                    "schema_version": "1.1",
                    "event_type": ev_type,
                    "timestamp": base_ts,
                    "batch_id": batch_id,
                    "job_id": job_id,
                    "source": {"project": project, "user": user, "host": host},
                    "data": {"metrics": {"MSE": 0.1}},
                },
                headers=headers,
            )


async def _mk_user(client, username: str) -> tuple[str, str]:
    """Register a user, return (jwt, reporter_token)."""
    await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
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
# Basic listing + pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_jobs_list_when_no_batches(client):
    r = await client.get("/api/jobs")
    assert r.status_code == 200
    body = r.json()
    assert body == {"items": [], "total": 0, "page": 1, "page_size": 50}


@pytest.mark.asyncio
async def test_list_returns_jobs_with_batch_context(client):
    await _seed_batch_with_jobs(
        client, batch_id="b-ctx", project="proj-A", host="host-1", n_jobs=2,
    )
    r = await client.get("/api/jobs")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    # Each item carries job + batch context
    for it in body["items"]:
        assert it["project"] == "proj-A"
        assert it["host"] == "host-1"
        assert it["job"]["batch_id"] == "b-ctx"
        assert it["job"]["model"] == "transformer"


@pytest.mark.asyncio
async def test_pagination_slices_correctly(client):
    await _seed_batch_with_jobs(client, batch_id="b-page", n_jobs=5)
    # Page 1 of 2 returns 2 items, total=5
    r1 = await client.get("/api/jobs", params={"page": 1, "page_size": 2})
    body1 = r1.json()
    assert body1["total"] == 5
    assert len(body1["items"]) == 2
    assert body1["page"] == 1
    assert body1["page_size"] == 2

    # Page 3 returns the trailing 1 row
    r3 = await client.get("/api/jobs", params={"page": 3, "page_size": 2})
    assert r3.json()["total"] == 5
    assert len(r3.json()["items"]) == 1


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_filter(client):
    await _seed_batch_with_jobs(
        client,
        batch_id="b-st",
        n_jobs=3,
        statuses=["done", "failed", "running"],
    )
    r = await client.get("/api/jobs", params={"status": "running"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["job"]["status"] == "running"

    r2 = await client.get("/api/jobs", params={"status": "FAILED"})
    # case-insensitive match
    assert r2.json()["total"] == 1


@pytest.mark.asyncio
async def test_project_and_host_filter(client):
    await _seed_batch_with_jobs(
        client, batch_id="b-A1", project="alpha", host="h-a", n_jobs=2,
    )
    await _seed_batch_with_jobs(
        client, batch_id="b-B1", project="beta", host="h-b", n_jobs=1,
    )

    r = await client.get("/api/jobs", params={"project": "alpha"})
    assert r.json()["total"] == 2
    r = await client.get("/api/jobs", params={"host": "h-b"})
    assert r.json()["total"] == 1
    # Combined filters → AND semantics; no rows match.
    r = await client.get(
        "/api/jobs", params={"project": "alpha", "host": "h-b"},
    )
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_batch_id_filter(client):
    await _seed_batch_with_jobs(client, batch_id="b-keep", n_jobs=2)
    await _seed_batch_with_jobs(client, batch_id="b-skip", n_jobs=2)
    r = await client.get("/api/jobs", params={"batch_id": "b-keep"})
    body = r.json()
    assert body["total"] == 2
    for it in body["items"]:
        assert it["job"]["batch_id"] == "b-keep"


@pytest.mark.asyncio
async def test_since_relative_shorthand(client):
    # Seed two batches: one in the deep past, one fresh.
    old_ts = _utcnow() - timedelta(days=7)
    await _seed_batch_with_jobs(
        client, batch_id="b-old", n_jobs=1, job_start=old_ts,
    )
    await _seed_batch_with_jobs(client, batch_id="b-new", n_jobs=1)
    # ``since=24h`` keeps only the fresh job.
    r = await client.get("/api/jobs", params={"since": "24h"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["job"]["batch_id"] == "b-new"


# ---------------------------------------------------------------------------
# RBAC / visibility — non-admin sees only own + shared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rbac_non_admin_sees_only_visible(client):
    """A second registered user starts out non-admin and must NOT see
    the admin-tester's jobs unless explicitly shared.
    """
    # Tester (admin) seeds a private batch
    await _seed_batch_with_jobs(client, batch_id="b-priv", n_jobs=2)

    bob_jwt, bob_token = await _mk_user(client, "bob")
    bob_hdr = {"Authorization": f"Bearer {bob_token}"}
    bob_jwt_hdr = {"Authorization": f"Bearer {bob_jwt}"}

    # Bob lists jobs — empty (no shared batches yet).
    r = await client.get("/api/jobs", headers=bob_jwt_hdr)
    assert r.json()["total"] == 0

    # Tester also seeds a batch as bob's reporter (bob owns it).
    await _seed_batch_with_jobs(
        client, batch_id="b-bob", user="bob", n_jobs=1, headers=bob_hdr,
    )
    # Clear the response cache: bob's first list call (which returned an
    # empty body) is cached for 10s and would mask the new owned batch
    # on the second call.
    from backend.utils.response_cache import default_cache as _c
    _c.clear()
    r = await client.get("/api/jobs", headers=bob_jwt_hdr)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["job"]["batch_id"] == "b-bob"

    # Tester shares b-priv with bob → bob now sees both.
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/batches/b-priv/shares",
        json={"grantee_username": "bob", "permission": "viewer"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code in (200, 201), r.text
    # Cache bust — the per-user response cache is keyed on user id, but a
    # new share arriving mid-test should be visible to bob immediately.
    from backend.utils.response_cache import default_cache as _c
    _c.clear()
    r = await client.get("/api/jobs", headers=bob_jwt_hdr)
    assert r.json()["total"] == 3  # 1 own + 2 shared


@pytest.mark.asyncio
async def test_rbac_project_share_grants_jobs(client):
    """A project-share viewer should see every job in shared projects."""
    # Tester seeds two projects.
    await _seed_batch_with_jobs(
        client, batch_id="b-pa", project="alpha", n_jobs=2,
    )
    await _seed_batch_with_jobs(
        client, batch_id="b-pb", project="beta", n_jobs=1,
    )
    bob_jwt, _ = await _mk_user(client, "bob")
    bob_jwt_hdr = {"Authorization": f"Bearer {bob_jwt}"}

    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/projects/shares",
        json={
            "project": "alpha",
            "grantee_username": "bob",
            "permission": "viewer",
        },
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code in (200, 201), r.text
    from backend.utils.response_cache import default_cache as _c
    _c.clear()
    r = await client.get("/api/jobs", headers=bob_jwt_hdr)
    body = r.json()
    # Only the alpha-project's 2 jobs surface; the beta batch stays hidden.
    assert body["total"] == 2
    for it in body["items"]:
        assert it["project"] == "alpha"


@pytest.mark.asyncio
async def test_unauth_returns_401(unauthed_client):
    r = await unauthed_client.get("/api/jobs")
    assert r.status_code == 401
