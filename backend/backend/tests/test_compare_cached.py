"""TTL-cache coverage for ``GET /api/compare``.

The Compare page polls /api/compare repeatedly when the user adds a
batch to the pin pool, refreshes, or hits the page from a deep link
twice within a few seconds. With N=32 (the upper bound) every uncached
call drives a visibility resolve + jobs IN-list query; these tests
verify the response cache absorbs the second call within the 10 s TTL.

Mirrors the patterns in ``test_batch_endpoints_cached.py``:

1. Two back-to-back calls with the same id list → loader runs once.
2. Different id orderings (a,b vs b,a) collide on the same cache key
   because we sort before keying.
3. Different users with the same id list keep separate cache entries.
"""
from __future__ import annotations

import pytest

from backend.tests._dashboard_helpers import (
    mk_user_with_token,
    seed_completed_batch,
)
from backend.utils.response_cache import default_cache as _response_cache


@pytest.mark.asyncio
async def test_compare_second_call_within_ttl_hits_cache(client, monkeypatch):
    """Two GETs to /api/compare with the same ids → loader runs exactly once."""
    await seed_completed_batch(client, batch_id="cmp-1", metrics={"MSE": 0.1})
    await seed_completed_batch(client, batch_id="cmp-2", metrics={"MSE": 0.2})

    # Reset to drop any cache entries seeded by the helper's POSTs.
    _response_cache.clear()

    load_count = 0
    seen_keys: list[str] = []
    orig = _response_cache.get_or_compute

    async def counting(key, loader):
        seen_keys.append(key)

        async def wrapped():
            nonlocal load_count
            load_count += 1
            return await loader()

        return await orig(key, wrapped)

    monkeypatch.setattr(_response_cache, "get_or_compute", counting)

    r1 = await client.get("/api/compare?batches=cmp-1,cmp-2")
    assert r1.status_code == 200, r1.text
    r2 = await client.get("/api/compare?batches=cmp-1,cmp-2")
    assert r2.status_code == 200

    # Same payload either way.
    assert r1.json() == r2.json()

    # Inspect just the compare-keyed traffic (other endpoints can show up
    # in seen_keys via shared helpers).
    compare_keys = [k for k in seen_keys if k.startswith("compare:")]
    # Two GETs route through the cache facade…
    assert len(compare_keys) == 2
    # …but only the first should actually run the loader.
    assert load_count == 1, (
        f"Expected one loader run within TTL; got {load_count}. "
        f"Compare keys: {compare_keys}"
    )


@pytest.mark.asyncio
async def test_compare_cache_key_collapses_id_order(client, monkeypatch):
    """``a,b`` and ``b,a`` share the same cache slot — sorted before keying.

    Without this, the Compare page's selection-edit flow (drag-reorder,
    deep-link re-entry) would miss the cache on every reordering.
    """
    await seed_completed_batch(client, batch_id="cmp-A")
    await seed_completed_batch(client, batch_id="cmp-B")

    _response_cache.clear()

    load_count = 0
    orig = _response_cache.get_or_compute

    async def counting(key, loader):
        async def wrapped():
            nonlocal load_count
            load_count += 1
            return await loader()

        return await orig(key, wrapped)

    monkeypatch.setattr(_response_cache, "get_or_compute", counting)

    r1 = await client.get("/api/compare?batches=cmp-A,cmp-B")
    assert r1.status_code == 200
    r2 = await client.get("/api/compare?batches=cmp-B,cmp-A")
    assert r2.status_code == 200

    # Loader should only run for the first ordering — the second hits the
    # sorted-id cache slot.
    assert load_count == 1


@pytest.mark.asyncio
async def test_compare_cache_keys_are_per_user(client, monkeypatch):
    """Two users that can both see the same batches get independent slots.

    Per-user keying is the safety net against visibility leaks: even if
    two callers ask for the same id list, they hit separate cache rows
    so a permission change on one user's side never serves stale data
    to the other.
    """
    # Tester (default client auth, admin) seeds two batches.
    await seed_completed_batch(client, batch_id="cmp-X", metrics={"MSE": 0.1})
    await seed_completed_batch(client, batch_id="cmp-Y", metrics={"MSE": 0.2})

    # Bob registers + gets shared access to both.
    tester_jwt = client._test_default_jwt  # type: ignore[attr-defined]
    bob_jwt, _ = await mk_user_with_token(client, "bob")
    for bid in ("cmp-X", "cmp-Y"):
        r = await client.post(
            f"/api/batches/{bid}/shares",
            json={"grantee_username": "bob", "permission": "viewer"},
            headers={"Authorization": f"Bearer {tester_jwt}"},
        )
        assert r.status_code == 201, r.text

    _response_cache.clear()

    seen_keys: list[str] = []
    orig = _response_cache.get_or_compute

    async def spying(key, loader):
        seen_keys.append(key)
        return await orig(key, loader)

    monkeypatch.setattr(_response_cache, "get_or_compute", spying)

    r_t = await client.get("/api/compare?batches=cmp-X,cmp-Y")
    assert r_t.status_code == 200, r_t.text
    r_b = await client.get(
        "/api/compare?batches=cmp-X,cmp-Y",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    assert r_b.status_code == 200, r_b.text

    compare_keys = [k for k in seen_keys if k.startswith("compare:")]
    assert len(compare_keys) == 2
    assert len(set(compare_keys)) == 2, (
        f"Per-user cache keys must differ for two distinct users: {compare_keys}"
    )
