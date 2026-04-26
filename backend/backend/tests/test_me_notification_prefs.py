"""Tests for ``GET / PUT /api/me/notification_prefs`` (#108)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


async def _jwt_headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {client._test_default_jwt}"}


# Canonical defaults pinned by the API — the test asserts them explicitly
# so a quiet drift in :data:`backend.api.me.DEFAULT_PREFS` becomes a
# loud failure rather than a silent UX change.
EXPECTED_DEFAULTS: dict[str, bool] = {
    "notify_batch_done": True,
    "notify_batch_failed": True,
    "notify_job_failed": True,
    "notify_diverged": True,
    "notify_job_idle": False,
}


async def test_get_returns_defaults_when_unset(client):
    """Fresh user (column NULL) should see the canonical defaults."""
    r = await client.get(
        "/api/me/notification_prefs", headers=await _jwt_headers(client)
    )
    assert r.status_code == 200, r.text
    assert r.json() == EXPECTED_DEFAULTS


async def test_put_persists_and_round_trips(client):
    """A PUT with all five keys round-trips via a follow-up GET."""
    new_prefs = {
        "notify_batch_done": False,
        "notify_batch_failed": True,
        "notify_job_failed": False,
        "notify_diverged": True,
        "notify_job_idle": True,
    }
    r = await client.put(
        "/api/me/notification_prefs",
        headers=await _jwt_headers(client),
        json=new_prefs,
    )
    assert r.status_code == 200, r.text
    assert r.json() == new_prefs

    # Follow-up GET reflects the persisted state.
    r2 = await client.get(
        "/api/me/notification_prefs", headers=await _jwt_headers(client)
    )
    assert r2.status_code == 200
    assert r2.json() == new_prefs

    # Sanity: the JSON column is the canonical compact-JSON form.
    import backend.db as db_mod
    from backend.models import User

    async with db_mod.SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == "tester"))
        ).scalar_one()
        assert user.notification_prefs_json is not None
        decoded = json.loads(user.notification_prefs_json)
        assert decoded == new_prefs


async def test_put_rejects_missing_keys(client):
    """Total-update semantics: missing keys → 422 from pydantic."""
    r = await client.put(
        "/api/me/notification_prefs",
        headers=await _jwt_headers(client),
        json={"notify_batch_done": True},
    )
    assert r.status_code == 422, r.text


async def test_put_rejects_unknown_keys(client):
    """``extra='forbid'`` rejects typos / extension attempts loudly."""
    body = {**EXPECTED_DEFAULTS, "notify_misspelled": True}
    r = await client.put(
        "/api/me/notification_prefs",
        headers=await _jwt_headers(client),
        json=body,
    )
    assert r.status_code == 422, r.text


async def test_endpoints_require_auth(client):
    """Both verbs are 401 without a bearer token."""
    client.headers.pop("Authorization", None)
    r1 = await client.get("/api/me/notification_prefs")
    assert r1.status_code == 401

    r2 = await client.put(
        "/api/me/notification_prefs", json=EXPECTED_DEFAULTS
    )
    assert r2.status_code == 401


async def test_corrupt_json_falls_back_to_defaults(client):
    """A hand-edited / corrupted column shouldn't 500 the GET path."""
    import backend.db as db_mod
    from backend.models import User

    async with db_mod.SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == "tester"))
        ).scalar_one()
        user.notification_prefs_json = "not-valid-json{"
        await session.commit()

    r = await client.get(
        "/api/me/notification_prefs", headers=await _jwt_headers(client)
    )
    assert r.status_code == 200
    assert r.json() == EXPECTED_DEFAULTS
