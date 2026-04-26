"""Anonymous-access audit for the Team Unify / public-demo surface.

The public UI (``/demo`` + ``/public/<slug>``) lets unauthenticated
visitors browse explicitly-published content. Everything else — write
endpoints, private reads, admin surface — MUST reject anonymous callers
with 401 before any business logic runs. Frontend ``readOnly`` gating is
cosmetic only; this file is the server-side source of truth.

Organisation:

* :func:`test_anonymous_*_requires_auth` — negative: anon → 401/403.
* :func:`test_anonymous_public_*` — positive: legitimate anon read paths.
* :func:`test_public_share_visible_to_anon` — end-to-end public share.

The ``unauthed_client`` fixture ships a bare ``AsyncClient`` with no
``Authorization`` header; the ``client`` fixture ships the default
reporter-token auth. For public-share tests we reuse ``client`` (to
seed the share via the owner) then strip the header mid-test.
"""
from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _batch_start_event(batch_id: str, project: str = "proj") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-23T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": project, "host": "h1"},
        "data": {"n_total_jobs": 1, "experiment_type": "forecast"},
    }


def _job_start_event(batch_id: str, job_id: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "job_start",
        "timestamp": "2026-04-23T09:01:00Z",
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": "proj", "host": "h1"},
        "data": {"model": "transformer", "dataset": "etth1"},
    }


async def _seed_public_share(client, batch_id: str = "anon-pub-1") -> str:
    """Seed a batch via the default reporter-token client, then mint a
    public share slug via the owner's JWT. Returns the slug.

    Caller is responsible for stripping ``Authorization`` before the
    subsequent anonymous GET.
    """
    r = await client.post("/api/events", json=_batch_start_event(batch_id))
    assert r.status_code == 200, r.text
    await client.post("/api/events", json=_job_start_event(batch_id, "j1"))

    jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        f"/api/batches/{batch_id}/public-share",
        json={},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["slug"]


# ---------------------------------------------------------------------------
# Write endpoints: every POST/PUT/PATCH/DELETE must 401 anonymously.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_post_events_is_401(unauthed_client):
    """Reporter ingest requires an em_live_ token — bare POST = 401."""
    r = await unauthed_client.post(
        "/api/events", json=_batch_start_event("anon-ev-1")
    )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_anonymous_post_events_batch_is_401(unauthed_client):
    r = await unauthed_client.post(
        "/api/events/batch",
        json={"events": [_batch_start_event("anon-ev-2")]},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_rerun_is_401(unauthed_client):
    r = await unauthed_client.post(
        "/api/batches/some-batch/rerun", json={"overrides": {}}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_stop_batch_is_401(unauthed_client):
    r = await unauthed_client.post("/api/batches/some-batch/stop")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_create_public_share_is_401(unauthed_client):
    r = await unauthed_client.post(
        "/api/batches/some-batch/public-share", json={}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_revoke_public_share_is_401(unauthed_client):
    r = await unauthed_client.delete(
        "/api/batches/some-batch/public-share/deadbeef"
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_batch_share_write_is_401(unauthed_client):
    r = await unauthed_client.post(
        "/api/batches/some-batch/shares",
        json={"username_or_email": "alice", "permission": "viewer"},
    )
    assert r.status_code == 401

    r = await unauthed_client.delete(
        "/api/batches/some-batch/shares/42"
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_project_share_write_is_401(unauthed_client):
    r = await unauthed_client.post(
        "/api/projects/shares",
        json={
            "project": "p",
            "grantee_username": "alice",
            "permission": "viewer",
        },
    )
    assert r.status_code == 401

    r = await unauthed_client.delete("/api/projects/shares/some-proj/42")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_token_crud_is_401(unauthed_client):
    r = await unauthed_client.post(
        "/api/tokens", json={"name": "x", "scope": "viewer"}
    )
    assert r.status_code == 401

    r = await unauthed_client.delete("/api/tokens/1")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_star_toggle_is_401(unauthed_client):
    r = await unauthed_client.post(
        "/api/stars",
        json={"target_type": "batch", "target_id": "b-1"},
    )
    assert r.status_code == 401

    r = await unauthed_client.delete("/api/stars/batch/b-1")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_pin_toggle_is_401(unauthed_client):
    r = await unauthed_client.post(
        "/api/pins", json={"batch_id": "b-1"}
    )
    assert r.status_code == 401

    r = await unauthed_client.delete("/api/pins/b-1")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_preferences_patch_is_401(unauthed_client):
    # Preferences sit under /api/users/me/preferences (prefix on router).
    r = await unauthed_client.patch(
        "/api/users/me/preferences", json={"hide_demo": True}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_notifications_write_is_401(unauthed_client):
    r = await unauthed_client.post("/api/notifications/mark_all_read")
    assert r.status_code == 401

    r = await unauthed_client.post("/api/notifications/1/ack")
    assert r.status_code == 401

    r = await unauthed_client.delete("/api/notifications/1")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_admin_writes_are_401(unauthed_client):
    """Admin surface requires JWT *and* is_admin — anon = 401 (auth first)."""
    r = await unauthed_client.post("/api/admin/users/1/ban")
    assert r.status_code == 401

    r = await unauthed_client.post(
        "/api/admin/projects/demo-proj/publish", json={}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_auth_logout_is_401(unauthed_client):
    """POST /api/auth/logout needs an authenticated session to revoke."""
    r = await unauthed_client.post("/api/auth/logout")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_session_revoke_is_401(unauthed_client):
    r = await unauthed_client.post("/api/auth/sessions/some-jti/revoke")
    assert r.status_code == 401


@pytest.mark.skip(
    reason=(
        "backend.api.artifacts.artifacts_router is currently not "
        "included in app.py (see test_artifacts.py failures in the "
        "baseline suite). When it is mounted, flip this back on — the "
        "auth dep is already wired on delete_artifact."
    )
)
@pytest.mark.asyncio
async def test_anonymous_artifact_delete_is_401(unauthed_client):
    r = await unauthed_client.delete("/api/artifacts/1")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Private read endpoints: anon GETs on authenticated-only reads = 401.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_list_batches_is_401(unauthed_client):
    r = await unauthed_client.get("/api/batches")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_list_batches_scope_mine_is_401(unauthed_client):
    """Even with scope=mine (which semantically requires auth), anon = 401."""
    r = await unauthed_client.get("/api/batches?scope=mine")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_get_private_batch_is_401(client, unauthed_client):
    """A batch *without* a public share is invisible to anon callers."""
    await client.post("/api/events", json=_batch_start_event("priv-1"))
    r = await unauthed_client.get("/api/batches/priv-1")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_list_jobs_is_401(unauthed_client):
    r = await unauthed_client.get("/api/batches/some-batch/jobs")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_job_detail_is_401(unauthed_client):
    r = await unauthed_client.get("/api/jobs/some-batch/some-job")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_list_projects_is_401(unauthed_client):
    """GET /api/projects is auth-gated; anon landing page uses /api/public/."""
    r = await unauthed_client.get("/api/projects")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_dashboard_is_401(unauthed_client):
    r = await unauthed_client.get("/api/dashboard")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_compare_is_401(unauthed_client):
    r = await unauthed_client.get(
        "/api/compare?batch_ids=a&batch_ids=b"
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Public read endpoints: anon should succeed (no auth header needed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_share_visible_to_anon(client):
    """End-to-end: seeded share → anon GET /api/public/{slug} returns 200."""
    slug = await _seed_public_share(client, "anon-pub-roundtrip")
    saved = client.headers.pop("Authorization", None)
    try:
        r = await client.get(f"/api/public/{slug}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == "anon-pub-roundtrip"
        # Owner PII must not leak on the anon path.
        assert "owner_id" not in body
        assert "email" not in body
        assert body["owner_label"].startswith("Shared")

        # Sibling endpoints also work without auth.
        r = await client.get(f"/api/public/{slug}/jobs")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_public_share_missing_slug_is_404(unauthed_client):
    """Anon GET on an unknown slug = 404, NOT 401."""
    r = await unauthed_client.get("/api/public/does-not-exist-slug")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_public_projects_list_is_public(unauthed_client):
    """GET /api/public/projects is the demo-landing list; always anon OK."""
    r = await unauthed_client.get("/api/public/projects")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_public_project_detail_unknown_is_404(unauthed_client):
    """Unpublished / nonexistent public project = 404 (never 401)."""
    r = await unauthed_client.get("/api/public/projects/nope-never")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_meta_hints_is_public(unauthed_client):
    """``/api/meta/hints`` serves static i18n copy — intentionally anonymous."""
    r = await unauthed_client.get("/api/meta/hints")
    assert r.status_code == 200
    body = r.json()
    assert "locale" in body
    assert "hints" in body


# ---------------------------------------------------------------------------
# Write-endpoint ordering: auth check runs before rate-limit. Anon floods
# of /api/events must surface 401, not 429.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_write_returns_401_not_429(unauthed_client):
    """Fire 5 anon POSTs in a row; each returns 401 (auth > rate-limit)."""
    for _ in range(5):
        r = await unauthed_client.post(
            "/api/events", json=_batch_start_event("burst")
        )
        assert r.status_code == 401, (
            "auth must precede rate-limit: "
            f"expected 401 on every burst hit, got {r.status_code}"
        )


@pytest.mark.asyncio
async def test_anonymous_write_with_garbage_bearer_is_401(unauthed_client):
    """A malformed Bearer value still 401s (not 500, not 200)."""
    r = await unauthed_client.post(
        "/api/events",
        json=_batch_start_event("garbage-bearer"),
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401
