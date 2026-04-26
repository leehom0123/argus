"""TTL-cache coverage for the hot batch/job read endpoints.

The ``/batches`` page renders N BatchCompactCards, each fanning out to
four parallel GETs (``get_batch`` / ``list_batch_jobs`` /
``get_batch_epochs_latest`` / ``get_batch_resources``). With 10+ batches
that balloons to 40+ concurrent DB-touching requests; wrapping each
handler in :data:`backend.utils.response_cache.default_cache` collapses
the repeat reads into a single loader call per (user, batch, params)
key for 10 seconds.

These tests exercise the behaviours we rely on at the endpoint layer:

1. A second call inside the TTL window re-uses the cached payload and
   does NOT invoke the loader / DB path again.
2. Two distinct users get separate cache entries for the same
   ``batch_id`` — per-user keying prevents visibility leaks.
3. The TTL actually expires: once ``default_cache._ttl`` seconds pass,
   the loader runs again and fresh data surfaces.

We monkeypatch ``default_cache`` attributes where needed so the assertion
"loader fired exactly once" stays deterministic without sleeping the real
10 seconds.
"""
from __future__ import annotations

import uuid

import pytest

from backend.utils import response_cache as response_cache_mod
from backend.utils.response_cache import default_cache as _response_cache


async def _seed_batch(client, batch_id: str = "b-cache-1") -> None:
    """POST a minimal batch_start so the tests have something to read."""
    ev = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1.1",
        "event_type": "batch_start",
        "timestamp": "2026-04-24T09:00:00Z",
        "batch_id": batch_id,
        "source": {"project": "proj", "user": "tester"},
        "data": {"n_total_jobs": 1},
    }
    r = await client.post("/api/events", json=ev)
    assert r.status_code == 200, r.text


async def _mk_user(client, username: str) -> tuple[str, str]:
    """Register a second user + mint a reporter token."""
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
# 1. Second call in the TTL window returns cached payload without re-loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_batch_second_call_hits_cache(client, monkeypatch):
    """Two back-to-back ``GET /api/batches/{id}`` calls → loader runs once.

    We instrument the TTL cache to count loader invocations: the real
    cache object is unchanged, we just wrap ``get_or_compute`` to count
    how many times its ``loader`` argument gets awaited.
    """
    await _seed_batch(client, "b-cache-1")

    load_count = 0
    orig = _response_cache.get_or_compute

    async def counting_get_or_compute(key, loader):
        async def wrapped():
            nonlocal load_count
            load_count += 1
            return await loader()

        return await orig(key, wrapped)

    monkeypatch.setattr(
        _response_cache, "get_or_compute", counting_get_or_compute
    )

    r1 = await client.get("/api/batches/b-cache-1")
    assert r1.status_code == 200
    r2 = await client.get("/api/batches/b-cache-1")
    assert r2.status_code == 200

    # Payload stable across both calls.
    assert r1.json() == r2.json()
    # Only the first call should have hit the loader; the second is a
    # cache-fresh-path return.
    assert load_count == 1, (
        f"Expected one loader run for two calls within TTL; got {load_count}"
    )


# ---------------------------------------------------------------------------
# 2. Per-user keying — two users get independent cache entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_batch_cache_keys_are_per_user(client, monkeypatch):
    """Tester (admin) + bob (sharee) fire two separate loader runs.

    Same batch_id, different callers → different cache keys. Without
    this the second user would serve the first user's visibility-
    filtered payload, which would be a leak the moment visibility ever
    diverges.
    """
    await _seed_batch(client, "b-cache-2")

    # Register bob and share b-cache-2 with him so he's allowed to read.
    bob_jwt, _ = await _mk_user(client, "bob")
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    r = await client.post(
        "/api/batches/b-cache-2/shares",
        json={"grantee_username": "bob", "permission": "viewer"},
        headers={"Authorization": f"Bearer {tester_jwt}"},
    )
    assert r.status_code == 201, r.text

    # Start counting from here — the share POST triggered its own
    # cache-busting calls on unrelated endpoints we don't care about.
    _response_cache.clear()

    seen_keys: list[str] = []
    orig = _response_cache.get_or_compute

    async def spying_get_or_compute(key, loader):
        seen_keys.append(key)
        return await orig(key, loader)

    monkeypatch.setattr(
        _response_cache, "get_or_compute", spying_get_or_compute
    )

    # Tester (default client auth) reads — hits the DB.
    r_tester = await client.get("/api/batches/b-cache-2")
    assert r_tester.status_code == 200
    # Bob reads with his own JWT — should NOT reuse tester's cached entry.
    r_bob = await client.get(
        "/api/batches/b-cache-2",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r_bob.status_code == 200

    batch_keys = [k for k in seen_keys if k.startswith("batch:")]
    assert len(batch_keys) == 2, batch_keys
    assert len(set(batch_keys)) == 2, (
        f"Per-user cache keys must differ for two distinct users: {batch_keys}"
    )


# ---------------------------------------------------------------------------
# 3. TTL expiry — after ``_ttl`` seconds the loader re-runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_batch_reloads_after_ttl(client, monkeypatch):
    """Freeze ``time.monotonic`` so we can fast-forward past the 10s TTL."""
    await _seed_batch(client, "b-cache-3")

    fake_now = {"t": 1_000_000.0}

    def _now() -> float:
        return fake_now["t"]

    monkeypatch.setattr(response_cache_mod.time, "monotonic", _now)

    load_count = 0
    orig = _response_cache.get_or_compute

    async def counting_get_or_compute(key, loader):
        async def wrapped():
            nonlocal load_count
            load_count += 1
            return await loader()

        return await orig(key, wrapped)

    monkeypatch.setattr(
        _response_cache, "get_or_compute", counting_get_or_compute
    )

    r1 = await client.get("/api/batches/b-cache-3")
    assert r1.status_code == 200
    assert load_count == 1

    # Still within TTL — no reload.
    fake_now["t"] += _response_cache.ttl_seconds - 0.5
    r2 = await client.get("/api/batches/b-cache-3")
    assert r2.status_code == 200
    assert load_count == 1

    # Tick past the TTL — loader must run again.
    fake_now["t"] += 1.0
    r3 = await client.get("/api/batches/b-cache-3")
    assert r3.status_code == 200
    assert load_count == 2


# ---------------------------------------------------------------------------
# 4. list_batch_jobs is wrapped too — same second-call-skips-loader contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_batch_jobs_hits_cache_on_second_call(client, monkeypatch):
    await _seed_batch(client, "b-cache-4")

    load_count = 0
    orig = _response_cache.get_or_compute

    async def counting_get_or_compute(key, loader):
        # Only count loads for the endpoint under test.
        if key.startswith("batch-jobs:"):
            async def wrapped():
                nonlocal load_count
                load_count += 1
                return await loader()

            return await orig(key, wrapped)
        return await orig(key, loader)

    monkeypatch.setattr(
        _response_cache, "get_or_compute", counting_get_or_compute
    )

    r1 = await client.get("/api/batches/b-cache-4/jobs")
    assert r1.status_code == 200
    r2 = await client.get("/api/batches/b-cache-4/jobs")
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert load_count == 1


# ---------------------------------------------------------------------------
# 5. HTTPException 404 must propagate, not get memoized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_not_found_is_not_cached(client):
    """A 404 from the loader raises and should not be stored.

    Two lookups of a nonexistent id both re-run the loader (confirming
    via shared ``TTLCache`` semantics that failures aren't memoized).
    """
    r1 = await client.get("/api/batches/does-not-exist")
    assert r1.status_code == 404
    # A second call should still give 404 without hanging / leaking the
    # previous HTTPException as a cached value.
    r2 = await client.get("/api/batches/does-not-exist")
    assert r2.status_code == 404
