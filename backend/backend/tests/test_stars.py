"""Tests for ``/api/stars`` — per-user favourites."""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import mk_user_with_token


@pytest.mark.asyncio
async def test_star_project_roundtrip(client):
    """POST → GET → DELETE → GET."""
    r = await client.post(
        "/api/stars",
        json={"target_type": "project", "target_id": "deepts"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_type"] == "project"
    assert body["target_id"] == "deepts"
    assert body["starred_at"]

    # GET includes the new row
    r = await client.get("/api/stars")
    assert r.status_code == 200
    assert any(
        s["target_type"] == "project" and s["target_id"] == "deepts"
        for s in r.json()
    )

    # DELETE → 204
    r = await client.delete("/api/stars/project/deepts")
    assert r.status_code == 204

    # GET no longer returns it
    r = await client.get("/api/stars")
    assert r.status_code == 200
    assert not any(
        s["target_type"] == "project" and s["target_id"] == "deepts"
        for s in r.json()
    )


@pytest.mark.asyncio
async def test_star_is_idempotent(client):
    """Re-POSTing the same star returns 200 (not 409)."""
    body = {"target_type": "batch", "target_id": "b-1"}
    r1 = await client.post("/api/stars", json=body)
    assert r1.status_code == 200
    first_ts = r1.json()["starred_at"]

    r2 = await client.post("/api/stars", json=body)
    assert r2.status_code == 200
    # Second POST returns the original row's timestamp unchanged.
    assert r2.json()["starred_at"] == first_ts

    # Still only one row in the list.
    r = await client.get("/api/stars")
    assert sum(
        1 for s in r.json()
        if s["target_id"] == "b-1" and s["target_type"] == "batch"
    ) == 1


@pytest.mark.asyncio
async def test_star_visibility_is_per_user(client):
    """Stars are private: Bob can't see Alice's stars."""
    # Tester stars "deepts".
    await client.post(
        "/api/stars",
        json={"target_type": "project", "target_id": "deepts"},
    )

    bob_jwt, _ = await mk_user_with_token(client, "bob")
    r = await client.get(
        "/api/stars",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_delete_missing_star_is_noop(client):
    """DELETE on a non-existent star is 204 not 404."""
    r = await client.delete("/api/stars/project/does-not-exist")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_star_rejects_bad_target_type(client):
    """Unknown target_type on POST → 422 (Pydantic); on DELETE → 400."""
    r = await client.post(
        "/api/stars",
        json={"target_type": "weirdo", "target_id": "x"},
    )
    assert r.status_code == 422

    r = await client.delete("/api/stars/weirdo/whatever")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_stars_requires_auth(unauthed_client):
    """Unauthenticated → 401."""
    r = await unauthed_client.get("/api/stars")
    assert r.status_code == 401
    r = await unauthed_client.post(
        "/api/stars", json={"target_type": "project", "target_id": "x"}
    )
    assert r.status_code == 401
