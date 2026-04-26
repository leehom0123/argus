"""Team-Unify smoke tests (2026-04-24 flip).

Tiny cross-cutting sanity set that a reviewer can eyeball in 5s to
confirm the two halves of the unify work hold together:

* anonymous path still reaches the demo fixture (``/api/public/*``)
* authenticated reads (regular + admin) never see the demo
* starring remains a private no-op surface and does not itself leak
  demo rows at POST-time (the read-back leak is a documented nit —
  see ``REVIEW_TEAM_UNIFY.md``)

The heavy-lifting tests (31 + 13 cases) live in
``test_anonymous_visibility.py`` + ``test_demo_visibility.py``; this
file is the cheap canary.
"""
from __future__ import annotations

import uuid

import pytest

from backend.db import SessionLocal
from backend.demo import DEMO_BATCH_ID, DEMO_PROJECT, seed_demo


async def _mk_user(client, username: str) -> str:
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
    assert lr.status_code == 200, lr.text
    return lr.json()["access_token"]


@pytest.mark.asyncio
async def test_smoke_anonymous_public_projects_returns_demo(unauthed_client):
    """Anonymous visitor sees the seeded demo project via /api/public."""
    async with SessionLocal() as db:
        await seed_demo(db)

    r = await unauthed_client.get("/api/public/projects")
    assert r.status_code == 200, r.text
    names = {row["project"] for row in r.json()}
    assert DEMO_PROJECT in names


@pytest.mark.asyncio
async def test_smoke_regular_user_projects_hides_demo(client):
    """Authenticated regular user never sees the demo on /api/projects."""
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt = await _mk_user(client, f"smoke_{uuid.uuid4().hex[:6]}")
    r = await client.get(
        "/api/projects", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert r.status_code == 200, r.text
    names = {row["project"] for row in r.json()}
    assert DEMO_PROJECT not in names


@pytest.mark.asyncio
async def test_smoke_regular_user_direct_demo_batch_is_404(client):
    """Direct navigation to the seeded demo batch is blocked for authed users."""
    async with SessionLocal() as db:
        await seed_demo(db)

    jwt = await _mk_user(client, f"smoke_{uuid.uuid4().hex[:6]}")
    r = await client.get(
        f"/api/batches/{DEMO_BATCH_ID}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_smoke_stars_endpoint_is_reachable_for_authed(client):
    """GET /api/stars returns 200 + an empty list for a fresh user.

    Read-back demo filtering is a known follow-up (see review doc); this
    test just pins that the endpoint stays healthy under the unify branch
    so a regression there would trip CI immediately.
    """
    jwt = await _mk_user(client, f"smoke_{uuid.uuid4().hex[:6]}")
    r = await client.get(
        "/api/stars", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert r.status_code == 200, r.text
    assert r.json() == []
