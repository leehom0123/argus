"""Admin-controlled public-demo project endpoints.

Covers: admin publish/unpublish, 403 for non-admin, anonymous reads,
404 hiding for unpublished projects, rate-limit, and the
``published_at`` reset behaviour.
"""
from __future__ import annotations

import uuid

import pytest


def _batch_event(batch_id: str, project: str = "demo-proj") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": project, "host": "h1"},
        "data": {"n_total_jobs": 1, "experiment_type": "forecast"},
    }


async def _register_second_user(client) -> str:
    """Register a second (non-admin) user and return their JWT."""
    saved = client.headers.pop("Authorization", None)
    try:
        reg = await client.post(
            "/api/auth/register",
            json={
                "username": "alice",
                "email": "alice@example.com",
                "password": "password123",
            },
        )
        assert reg.status_code == 201, reg.text
        login = await client.post(
            "/api/auth/login",
            json={
                "username_or_email": "alice",
                "password": "password123",
            },
        )
        assert login.status_code == 200, login.text
        return login.json()["access_token"]
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_admin_publishes_and_anon_can_read(client):
    """Admin publish → anonymous can list + read detail + leaderboard."""
    # Tester is the first user → admin. Seed a batch so the project exists.
    await client.post("/api/events", json=_batch_event("b-1", "demo-proj"))
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r = await client.post(
        "/api/admin/projects/demo-proj/publish",
        json={"description": "Benchmark sweep over 12 models"},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"] == "demo-proj"
    assert body["is_public"] is True
    assert body["public_description"] == "Benchmark sweep over 12 models"
    assert body["published_at"] is not None
    assert body["published_by_user_id"] is not None

    # Anonymous: strip the API token default header.
    saved = client.headers.pop("Authorization", None)
    try:
        lst = await client.get("/api/public/projects")
        assert lst.status_code == 200, lst.text
        entries = lst.json()
        # Platform may seed a built-in demo project; assert our
        # publication is present rather than being the only row.
        ours = [e for e in entries if e["project"] == "demo-proj"]
        assert len(ours) == 1, entries
        assert ours[0]["description"] == "Benchmark sweep over 12 models"
        assert ours[0]["n_batches"] == 1

        detail = await client.get("/api/public/projects/demo-proj")
        assert detail.status_code == 200
        d = detail.json()
        assert d["project"] == "demo-proj"
        assert d["n_batches"] == 1
        # No owner_id / email leaked into the anon detail shape.
        assert "owner_id" not in d
        assert "owners" not in d

        lb = await client.get("/api/public/projects/demo-proj/leaderboard")
        assert lb.status_code == 200
        assert isinstance(lb.json(), list)

        mx = await client.get("/api/public/projects/demo-proj/matrix")
        assert mx.status_code == 200
        assert mx.json()["project"] == "demo-proj"

        ab = await client.get(
            "/api/public/projects/demo-proj/active-batches"
        )
        assert ab.status_code == 200
        for row in ab.json():
            # Anonymous surface must never leak owner_id.
            assert row["owner_id"] is None

        res = await client.get("/api/public/projects/demo-proj/resources")
        assert res.status_code == 200

        bl = await client.get("/api/public/projects/demo-proj/batches")
        assert bl.status_code == 200
        assert bl.json()[0]["batch_id"] == "b-1"
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_non_admin_cannot_publish(client):
    """Second (non-admin) user gets 403 on publish / unpublish."""
    await client.post("/api/events", json=_batch_event("b-2", "p2"))
    alice_jwt = await _register_second_user(client)

    saved = client.headers.pop("Authorization", None)
    try:
        r = await client.post(
            "/api/admin/projects/p2/publish",
            json={"description": "x"},
            headers={"Authorization": f"Bearer {alice_jwt}"},
        )
        assert r.status_code == 403, r.text

        r = await client.post(
            "/api/admin/projects/p2/unpublish",
            headers={"Authorization": f"Bearer {alice_jwt}"},
        )
        assert r.status_code == 403
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_unpublish_hides_from_anon(client):
    """Unpublish → anon GET returns 404 (not 401)."""
    await client.post("/api/events", json=_batch_event("b-3", "p3"))
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    await client.post(
        "/api/admin/projects/p3/publish",
        json={"description": "temp"},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    r = await client.post(
        "/api/admin/projects/p3/unpublish",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 204

    saved = client.headers.pop("Authorization", None)
    try:
        # 404 — anon must not see private projects
        r = await client.get("/api/public/projects/p3")
        assert r.status_code == 404

        # Landing list is empty (or no longer includes p3)
        r = await client.get("/api/public/projects")
        assert r.status_code == 200
        assert all(e["project"] != "p3" for e in r.json())
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_anon_unknown_project_returns_404(client):
    """Anonymous hits nonexistent /api/public/projects/<x> → 404 (not 401)."""
    saved = client.headers.pop("Authorization", None)
    try:
        r = await client.get("/api/public/projects/does-not-exist")
        assert r.status_code == 404
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_anon_leaderboard_matches_admin(client):
    """Anonymous leaderboard equals admin's (ignoring owner-private filter)."""
    # Seed events with metrics so the leaderboard has at least one row.
    await client.post("/api/events", json=_batch_event("b-4", "p4"))
    await client.post(
        "/api/events",
        json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_start",
            "timestamp": "2026-04-23T09:01:00Z",
            "batch_id": "b-4",
            "job_id": "j1",
            "source": {"project": "p4"},
            "data": {"model": "transformer", "dataset": "etth1"},
        },
    )
    await client.post(
        "/api/events",
        json={
            "event_id": str(uuid.uuid4()),
            "schema_version": "1.1",
            "event_type": "job_done",
            "timestamp": "2026-04-23T09:01:30Z",
            "batch_id": "b-4",
            "job_id": "j1",
            "source": {"project": "p4"},
            "data": {
                "status": "DONE",
                "elapsed_s": 30,
                "metrics": {"MSE": 0.25, "MAE": 0.31},
            },
        },
    )
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post(
        "/api/admin/projects/p4/publish",
        json={"description": "mtc"},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )

    admin_lb = await client.get(
        "/api/projects/p4/leaderboard",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert admin_lb.status_code == 200

    saved = client.headers.pop("Authorization", None)
    try:
        anon_lb = await client.get("/api/public/projects/p4/leaderboard")
    finally:
        if saved:
            client.headers["Authorization"] = saved
    assert anon_lb.status_code == 200

    # Admin and anonymous rows must match for a wholly-public project
    # (every batch is owned by the admin, so no owner-filtering occurs).
    assert [r["job_id"] for r in admin_lb.json()] == [
        r["job_id"] for r in anon_lb.json()
    ]


@pytest.mark.asyncio
async def test_public_endpoints_rate_limited(client, monkeypatch):
    """61st anon request in a minute → 429 + Retry-After."""
    from backend.utils import ratelimit as rl
    from backend.utils.ratelimit import TokenBucket

    await client.post("/api/events", json=_batch_event("b-rl", "p-rl"))
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    await client.post(
        "/api/admin/projects/p-rl/publish",
        json={},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )

    tiny = TokenBucket(capacity=5, refill_per_sec=0.01)
    monkeypatch.setattr(rl, "_PUBLIC_BUCKET", tiny)
    monkeypatch.setattr(rl, "get_public_bucket", lambda: tiny)

    saved = client.headers.pop("Authorization", None)
    try:
        for _ in range(5):
            r = await client.get("/api/public/projects/p-rl")
            assert r.status_code == 200, r.text
        r = await client.get("/api/public/projects/p-rl")
        assert r.status_code == 429
        assert "retry-after" in {k.lower() for k in r.headers.keys()}
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_published_by_user_id_populated(client):
    """After publish, ProjectMeta.published_by_user_id = admin's id."""
    await client.post("/api/events", json=_batch_event("b-who", "p-who"))
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {admin_jwt}"}
    )
    assert me.status_code == 200
    admin_id = me.json()["id"]

    r = await client.post(
        "/api/admin/projects/p-who/publish",
        json={"description": "d"},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 200
    assert r.json()["published_by_user_id"] == admin_id

    # Admin list includes the row.
    lst = await client.get(
        "/api/admin/projects/public",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert lst.status_code == 200
    assert any(
        e["project"] == "p-who" and e["published_by_user_id"] == admin_id
        for e in lst.json()
    )


@pytest.mark.asyncio
async def test_publish_resets_published_at_on_each_cycle(client):
    """Unpublish → publish again refreshes ``published_at``.

    The intuitive behaviour for a Publish button is "every new publish
    is a new event". Admins who want to re-promote a project (e.g.
    after bumping the description) get a fresh published_at so the
    diagnostic list surfaces the most-recent change. The previous
    value is lost on purpose — the audit log keeps the history.
    """
    import asyncio

    await client.post("/api/events", json=_batch_event("b-cyc", "p-cyc"))
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    r1 = await client.post(
        "/api/admin/projects/p-cyc/publish",
        json={"description": "first"},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r1.status_code == 200
    first_ts = r1.json()["published_at"]

    await client.post(
        "/api/admin/projects/p-cyc/unpublish",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    # Give the clock at least a second so the ISO-truncated timestamp
    # can actually differ on re-publish.
    await asyncio.sleep(1.1)

    r2 = await client.post(
        "/api/admin/projects/p-cyc/publish",
        json={"description": "second"},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r2.status_code == 200
    second_ts = r2.json()["published_at"]
    assert first_ts is not None and second_ts is not None
    assert second_ts > first_ts
    assert r2.json()["public_description"] == "second"


@pytest.mark.asyncio
async def test_admin_sees_is_public_on_project_detail(client):
    """Admin GET /api/projects/<p> surfaces is_public + description."""
    await client.post("/api/events", json=_batch_event("b-det", "p-det"))
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]

    # Before publish — admin sees is_public=False.
    r = await client.get(
        "/api/projects/p-det",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 200
    assert r.json()["is_public"] is False
    assert r.json()["public_description"] is None

    await client.post(
        "/api/admin/projects/p-det/publish",
        json={"description": "detail-test"},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    r = await client.get(
        "/api/projects/p-det",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 200
    assert r.json()["is_public"] is True
    assert r.json()["public_description"] == "detail-test"


@pytest.mark.asyncio
async def test_publish_unknown_project_404(client):
    """Publishing a non-existent project name returns 404."""
    admin_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/admin/projects/does-not-exist/publish",
        json={"description": "x"},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert r.status_code == 404
