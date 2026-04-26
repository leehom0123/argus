"""End-to-end visibility tests for the demo fixture.

Establishes the 2026-04-24 rule:

* **Anonymous visitors** see the seeded demo via ``/api/public/*`` —
  that's the whole point of the demo.
* **Authenticated users** (regular AND admin) never see the demo on
  any of the primary read surfaces. Demo batches are excluded from
  ``/api/batches``, demo projects from ``/api/projects`` and
  ``/api/dashboard``, demo hosts from ``/api/hosts/.../timeseries``.
  Direct navigation to a demo entity returns 404.

The old ``User.hide_demo`` opt-out is retained as a no-op (tests
live in ``test_demo_seed.py``); this file focuses on the new
invariants themselves.
"""
from __future__ import annotations

import uuid

import pytest

from backend.db import SessionLocal
from backend.demo import DEMO_BATCH_ID, DEMO_HOST, DEMO_PROJECT, seed_demo


async def _mk_user(client, username: str) -> tuple[str, int]:
    """Register a non-admin user and return ``(jwt, user_id)``."""
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    user_id = reg.json()["user_id"]
    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": "password123"},
    )
    assert lr.status_code == 200, lr.text
    return lr.json()["access_token"], user_id


def _batch_start_event(
    batch_id: str, project: str = "regular", host: str = "regular-host"
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": project, "host": host},
        "data": {"n_total_jobs": 1, "experiment_type": "forecast"},
    }


async def _seed_regular_batch(
    client, jwt: str, batch_id: str, project: str, host: str = "regular-host"
) -> None:
    """Post a batch_start under ``jwt`` so it's owned by that user.

    We can't re-use the default reporter token because that's owned by
    the bootstrap admin; regular-user batches need their own token.
    """
    # Mint a reporter token for this user so the batch is owned by them.
    tok = await client.post(
        "/api/tokens",
        json={"name": f"tok-{batch_id}", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert tok.status_code == 201, tok.text
    reporter = tok.json()["token"]
    r = await client.post(
        "/api/events",
        json=_batch_start_event(batch_id, project=project, host=host),
        headers={"Authorization": f"Bearer {reporter}"},
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Anonymous surface still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_sees_demo_via_public_list(unauthed_client):
    """The canonical anon path: /api/public/projects returns the demo."""
    async with SessionLocal() as db:
        await seed_demo(db)

    r = await unauthed_client.get("/api/public/projects")
    assert r.status_code == 200, r.text
    projects = {row["project"] for row in r.json()}
    assert DEMO_PROJECT in projects


@pytest.mark.asyncio
async def test_anonymous_sees_demo_project_detail(unauthed_client):
    async with SessionLocal() as db:
        await seed_demo(db)

    r = await unauthed_client.get(f"/api/public/projects/{DEMO_PROJECT}")
    assert r.status_code == 200, r.text
    assert r.json()["project"] == DEMO_PROJECT


# ---------------------------------------------------------------------------
# Authenticated user — cannot see demo anywhere
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regular_user_projects_excludes_demo(client):
    """Non-admin GET /api/projects never lists the demo project."""
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt, _ = await _mk_user(client, "alice")
    # Seed a regular batch so the user has at least one project to see.
    await _seed_regular_batch(client, jwt, "reg-b1", "alice-proj")

    r = await client.get(
        "/api/projects", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert r.status_code == 200, r.text
    names = {row["project"] for row in r.json()}
    assert DEMO_PROJECT not in names
    assert "alice-proj" in names


@pytest.mark.asyncio
async def test_admin_projects_excludes_demo(client):
    """Admin GET /api/projects also never lists the demo project."""
    async with SessionLocal() as db:
        await seed_demo(db)

    # Bootstrap admin has the default reporter token + admin rights.
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post(
        "/api/events",
        json=_batch_start_event("admin-b1", project="admin-proj"),
    )

    r = await client.get(
        "/api/projects", headers={"Authorization": f"Bearer {admin_jwt}"}
    )
    assert r.status_code == 200, r.text
    names = {row["project"] for row in r.json()}
    assert DEMO_PROJECT not in names, (
        "admin must not see demo on the default list either"
    )
    assert "admin-proj" in names


@pytest.mark.asyncio
async def test_regular_user_batches_excludes_demo(client):
    """GET /api/batches (scope=all, default) hides demo batches."""
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt, _ = await _mk_user(client, "bob")
    await _seed_regular_batch(client, jwt, "reg-b2", "bob-proj")

    r = await client.get(
        "/api/batches", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert DEMO_BATCH_ID not in ids
    assert "reg-b2" in ids


@pytest.mark.asyncio
async def test_admin_batches_excludes_demo(client):
    """Admin GET /api/batches?scope=all also hides demo batches."""
    async with SessionLocal() as db:
        await seed_demo(db)

    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post(
        "/api/events",
        json=_batch_start_event("admin-b2", project="admin-proj2"),
    )

    r = await client.get(
        "/api/batches?scope=all",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert DEMO_BATCH_ID not in ids
    assert "admin-b2" in ids


@pytest.mark.asyncio
async def test_regular_user_direct_demo_project_is_404(client):
    """GET /api/projects/<demo> returns 404 for logged-in users."""
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt, _ = await _mk_user(client, "carol")
    r = await client.get(
        f"/api/projects/{DEMO_PROJECT}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_admin_direct_demo_project_is_404(client):
    """Admin direct nav to /api/projects/<demo> is also 404."""
    async with SessionLocal() as db:
        await seed_demo(db)

    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.get(
        f"/api/projects/{DEMO_PROJECT}",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_regular_user_direct_demo_batch_is_404(client):
    """GET /api/batches/<demo-batch> returns 404 for logged-in users."""
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt, _ = await _mk_user(client, "dan")
    r = await client.get(
        f"/api/batches/{DEMO_BATCH_ID}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_admin_direct_demo_batch_is_404(client):
    """Admin direct nav to /api/batches/<demo-batch> is also 404."""
    async with SessionLocal() as db:
        await seed_demo(db)

    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.get(
        f"/api/batches/{DEMO_BATCH_ID}",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_demo_host_timeseries_is_404(client):
    """GET /api/hosts/<demo-host>/timeseries returns 404 for logged-in users."""
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt, _ = await _mk_user(client, "erin")
    r = await client.get(
        f"/api/hosts/{DEMO_HOST}/timeseries",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_regular_user_dashboard_has_no_demo(client):
    """GET /api/dashboard returns no demo project / batch / host."""
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt, _ = await _mk_user(client, "frank")
    await _seed_regular_batch(client, jwt, "reg-b3", "frank-proj")

    r = await client.get(
        "/api/dashboard", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    project_names = {p["project"] for p in body["projects"]}
    assert DEMO_PROJECT not in project_names
    assert "frank-proj" in project_names
    # Host cards must not leak the demo host either.
    host_names = {h["host"] for h in body["hosts"]}
    assert DEMO_HOST not in host_names
    # Activity feed — no event should reference the demo batch.
    act_batches = {e["batch_id"] for e in body["activity"]}
    assert DEMO_BATCH_ID not in act_batches


@pytest.mark.asyncio
async def test_compare_refuses_demo_batch(client):
    """GET /api/compare with a demo batch id returns 404 (invisible)."""
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt, _ = await _mk_user(client, "gina")
    await _seed_regular_batch(client, jwt, "reg-cmp-1", "gina-proj")
    await _seed_regular_batch(client, jwt, "reg-cmp-2", "gina-proj")

    # Two visible batches → compare succeeds.
    r = await client.get(
        f"/api/compare?batches=reg-cmp-1,reg-cmp-2",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 200, r.text

    # Swap one for the demo batch → 404 (demo invisible).
    r = await client.get(
        f"/api/compare?batches=reg-cmp-1,{DEMO_BATCH_ID}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 404, r.text
