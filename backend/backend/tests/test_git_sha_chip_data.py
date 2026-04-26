"""Roadmap #18 — derived git_sha_short / git_remote_url on BatchOut.env_snapshot.

Backend side of the Git SHA chip: the batch detail endpoint enriches the
decoded ``env_snapshot`` with two extra fields so the frontend can build
``github_url = f"{git_remote_url}/commit/{git_sha}"`` without re-parsing.

Covered scenarios:
  1. Full env_snapshot with git_sha + HTTPS remote → both derived fields present
  2. SSH-style ``git@github.com:user/repo.git`` remote → normalised to HTTPS
  3. Missing git_remote → ``git_remote_url=None`` (but still exposed as a key)
  4. Missing git_sha → ``git_sha_short=None``
  5. Batch without any env_snapshot → ``env_snapshot=None`` (unchanged)
"""
from __future__ import annotations

import uuid

import pytest


_BATCH_ID_BASE = "b-gitchip"


def _make_event(event_type: str, batch_id: str, data: dict) -> dict:
    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": "2026-04-24T10:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "p", "host": "h", "user": "u"},
        "data": data,
    }


@pytest.mark.asyncio
async def test_git_sha_short_and_https_remote(client):
    """HTTPS git_remote + git_sha → both derived fields populated."""
    batch_id = f"{_BATCH_ID_BASE}-https"
    await client.post("/api/events", json=_make_event(
        "batch_start", batch_id, {"n_total_jobs": 1},
    ))
    await client.post("/api/events", json=_make_event(
        "env_snapshot", batch_id, {
            "git_sha": "abcdef0123456789abcdef",
            "git_branch": "main",
            "git_remote": "https://github.com/leehom0123/argus.git",
        },
    ))

    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200, r.text
    snap = r.json()["env_snapshot"]
    assert snap["git_sha_short"] == "abcdef01"
    assert snap["git_remote_url"] == (
        "https://github.com/leehom0123/argus"
    )
    # Original fields must still be present
    assert snap["git_sha"] == "abcdef0123456789abcdef"


@pytest.mark.asyncio
async def test_git_remote_ssh_form_is_normalised(client):
    """SSH-style remote ``git@host:user/repo.git`` → HTTPS URL."""
    batch_id = f"{_BATCH_ID_BASE}-ssh"
    await client.post("/api/events", json=_make_event(
        "batch_start", batch_id, {"n_total_jobs": 1},
    ))
    await client.post("/api/events", json=_make_event(
        "env_snapshot", batch_id, {
            "git_sha": "1234567890abcdef",
            "git_remote": "git@github.com:leehom0123/argus.git",
        },
    ))

    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    snap = r.json()["env_snapshot"]
    assert snap["git_remote_url"] == (
        "https://github.com/leehom0123/argus"
    )
    assert snap["git_sha_short"] == "12345678"


@pytest.mark.asyncio
async def test_missing_git_remote_returns_none(client):
    """No git_remote → git_remote_url is None, git_sha_short still derived."""
    batch_id = f"{_BATCH_ID_BASE}-noremote"
    await client.post("/api/events", json=_make_event(
        "batch_start", batch_id, {"n_total_jobs": 1},
    ))
    await client.post("/api/events", json=_make_event(
        "env_snapshot", batch_id, {
            "git_sha": "deadbeefcafef00d",
            # No git_remote field
        },
    ))

    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    snap = r.json()["env_snapshot"]
    assert snap["git_sha_short"] == "deadbeef"
    assert snap["git_remote_url"] is None


@pytest.mark.asyncio
async def test_missing_git_sha_returns_none_short(client):
    """No git_sha → git_sha_short=None, git_remote_url still derived."""
    batch_id = f"{_BATCH_ID_BASE}-nosha"
    await client.post("/api/events", json=_make_event(
        "batch_start", batch_id, {"n_total_jobs": 1},
    ))
    await client.post("/api/events", json=_make_event(
        "env_snapshot", batch_id, {
            "git_remote": "https://github.com/owner/repo",
            # no git_sha
        },
    ))

    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    snap = r.json()["env_snapshot"]
    assert snap["git_sha_short"] is None
    assert snap["git_remote_url"] == "https://github.com/owner/repo"


@pytest.mark.asyncio
async def test_batch_without_env_snapshot_is_unchanged(client):
    """Batch that never emitted env_snapshot → env_snapshot=None in response."""
    batch_id = f"{_BATCH_ID_BASE}-nosnap"
    await client.post("/api/events", json=_make_event(
        "batch_start", batch_id, {"n_total_jobs": 1},
    ))
    r = await client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["env_snapshot"] is None
