"""Unit tests for ``backend.utils.response_cache.TTLCache``.

Covers the four behaviours the API layer relies on:

1. A second call inside the TTL window hits the cache (loader fires once).
2. Different keys (e.g. different query strings) don't collide.
3. Once the TTL expires, the loader runs again.
4. Concurrent callers for the same key dedupe into one loader invocation.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.utils.response_cache import MAX_TTL_SECONDS, TTLCache


@pytest.mark.asyncio
async def test_ttl_cap_enforced_at_construction() -> None:
    TTLCache(1.0)  # ok
    TTLCache(MAX_TTL_SECONDS)  # edge ok

    with pytest.raises(ValueError):
        TTLCache(MAX_TTL_SECONDS + 0.01)
    with pytest.raises(ValueError):
        TTLCache(60.0)
    with pytest.raises(ValueError):
        TTLCache(0)
    with pytest.raises(ValueError):
        TTLCache(-1)


@pytest.mark.asyncio
async def test_second_call_hits_cache() -> None:
    """First call fires the loader; second call within TTL returns cached."""
    cache = TTLCache(10.0)
    call_count = 0

    async def loader() -> int:
        nonlocal call_count
        call_count += 1
        return 42

    v1 = await cache.get_or_compute("k", loader)
    v2 = await cache.get_or_compute("k", loader)

    assert v1 == 42
    assert v2 == 42
    assert call_count == 1, "Loader should have been invoked exactly once"


@pytest.mark.asyncio
async def test_different_keys_do_not_collide() -> None:
    cache = TTLCache(10.0)
    calls: list[str] = []

    def make_loader(tag: str):
        async def _load() -> str:
            calls.append(tag)
            return f"value-{tag}"

        return _load

    v_a = await cache.get_or_compute("metric=MSE", make_loader("A"))
    v_b = await cache.get_or_compute("metric=MAE", make_loader("B"))
    # Repeat both — should stay at one loader run per key.
    v_a2 = await cache.get_or_compute("metric=MSE", make_loader("A"))
    v_b2 = await cache.get_or_compute("metric=MAE", make_loader("B"))

    assert v_a == "value-A" and v_a2 == "value-A"
    assert v_b == "value-B" and v_b2 == "value-B"
    assert calls == ["A", "B"], "Each key should fire its own loader once"


@pytest.mark.asyncio
async def test_entry_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Using a monotonic-clock monkeypatch avoids real sleeps."""
    import backend.utils.response_cache as mod

    fake_now = {"t": 1000.0}

    def _now() -> float:
        return fake_now["t"]

    monkeypatch.setattr(mod.time, "monotonic", _now)

    cache = TTLCache(1.0)
    call_count = 0

    async def loader() -> int:
        nonlocal call_count
        call_count += 1
        return call_count

    # First call at t=1000.
    assert await cache.get_or_compute("k", loader) == 1

    # Advance just under 1s: still cached.
    fake_now["t"] = 1000.9
    assert await cache.get_or_compute("k", loader) == 1
    assert call_count == 1

    # Advance past TTL: loader should run again.
    fake_now["t"] = 1002.0
    assert await cache.get_or_compute("k", loader) == 2
    assert call_count == 2


@pytest.mark.asyncio
async def test_concurrent_calls_dedupe() -> None:
    """20 concurrent callers for the same key should trigger one loader."""
    cache = TTLCache(10.0)
    call_count = 0
    loader_started = asyncio.Event()
    release = asyncio.Event()

    async def loader() -> str:
        nonlocal call_count
        call_count += 1
        loader_started.set()
        # Block until the test releases us, so every caller is forced to
        # queue on the in-flight future rather than racing to start a
        # second loader.
        await release.wait()
        return "ok"

    tasks = [
        asyncio.create_task(cache.get_or_compute("shared", loader))
        for _ in range(20)
    ]
    await loader_started.wait()
    # All 20 tasks should now be awaiting the same future. Release.
    release.set()

    results = await asyncio.gather(*tasks)
    assert results == ["ok"] * 20
    assert call_count == 1, (
        f"Expected a single loader run under dedup, got {call_count}"
    )


@pytest.mark.asyncio
async def test_loader_exception_does_not_cache() -> None:
    """If the loader raises, every waiter gets the error and the next

    call re-runs the loader — we don't want to memoize a transient DB
    failure for 10 seconds.
    """
    cache = TTLCache(10.0)
    call_count = 0

    async def bad_loader() -> int:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await cache.get_or_compute("k", bad_loader)
    with pytest.raises(RuntimeError):
        await cache.get_or_compute("k", bad_loader)

    assert call_count == 2


@pytest.mark.asyncio
async def test_invalidate_forces_reload() -> None:
    cache = TTLCache(10.0)
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_compute("k", loader) == 1
    cache.invalidate("k")
    assert await cache.get_or_compute("k", loader) == 2


@pytest.mark.asyncio
async def test_invalidate_prefix() -> None:
    cache = TTLCache(10.0)

    async def loader_for(v: int):
        async def _l() -> int:
            return v

        return _l

    await cache.get_or_compute("dashboard:u1:mine", await loader_for(1))
    await cache.get_or_compute("dashboard:u1:all", await loader_for(2))
    await cache.get_or_compute("dashboard:u2:all", await loader_for(3))

    cache.invalidate_prefix("dashboard:u1:")

    # u1 entries gone; u2 retained.
    assert "dashboard:u1:mine" not in cache._store
    assert "dashboard:u1:all" not in cache._store
    assert "dashboard:u2:all" in cache._store
