"""In-process TTL response cache with in-flight deduplication.

Small, async-friendly wrapper that memoizes expensive read paths for a
fixed number of seconds. Intended for hot GET endpoints whose payloads
tolerate a few seconds of staleness (dashboard, public project list,
host timeseries, project leaderboard / matrix).

Design notes
------------
* **Hard-capped TTL.** Construction with ``ttl_seconds > 10`` raises
  ``ValueError``. This is deliberate: the cache is a read-through
  buffer for hot reads, not a correctness boundary. Keeping the cap
  small means drift after a write is bounded by ~10s even if we miss
  an explicit invalidation, so callers don't have to plumb fine-grained
  busts through every write endpoint.
* **In-flight dedup.** A concurrent second caller for the same key
  awaits the first caller's loader future instead of firing a second
  DB round-trip. This is the main reason the cache exists — a slow
  query under concurrency pileup turns into N loader runs without it.
* **Single process.** Uvicorn workers each have their own instance.
  Good enough for our single-worker prod deploy; multi-worker would
  want Redis or similar.
* **No eviction beyond TTL.** The store is flat ``dict`` so cardinality
  is caller-controlled. Endpoints already bound cache keys by path +
  user + query-string, which is finite.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = ["TTLCache", "default_cache", "MAX_TTL_SECONDS"]


MAX_TTL_SECONDS: float = 10.0


class TTLCache:
    """Async TTL cache with per-key in-flight deduplication.

    Parameters
    ----------
    ttl_seconds:
        Lifetime of a cached entry in seconds. Capped at
        :data:`MAX_TTL_SECONDS` — higher values raise ``ValueError``.
    """

    def __init__(self, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds must be <= {MAX_TTL_SECONDS}s "
                f"(got {ttl_seconds})"
            )
        self._ttl: float = float(ttl_seconds)
        # key -> (expires_at_monotonic, value)
        self._store: dict[str, tuple[float, Any]] = {}
        # key -> pending loader future (in-flight dedup)
        self._pending: dict[str, asyncio.Future[Any]] = {}
        # Guards mutation of _store + _pending. Loader runs outside
        # the lock so slow queries don't block cache lookups for
        # unrelated keys.
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    async def get_or_compute(
        self,
        key: str,
        loader: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Return cached value for *key*, or call *loader* and cache it.

        If the entry is fresh (not expired) it's returned immediately.
        If another coroutine is already loading *key*, this call waits
        on that future instead of running *loader* again.
        """
        now = time.monotonic()

        # Fast path: fresh cache hit, no lock contention beyond the
        # dict lookup itself.
        entry = self._store.get(key)
        if entry is not None and entry[0] > now:
            return entry[1]

        # Slow path: coordinate under the lock.
        async with self._lock:
            # Re-check under lock to avoid a thundering herd that
            # slipped past the first check.
            entry = self._store.get(key)
            if entry is not None and entry[0] > time.monotonic():
                return entry[1]

            pending = self._pending.get(key)
            if pending is not None:
                # Someone else is computing this right now — share
                # their future. We release the lock while awaiting.
                fut = pending
                owner = False
            else:
                fut = asyncio.get_event_loop().create_future()
                self._pending[key] = fut
                owner = True

        if not owner:
            # Wait outside the lock. The owner will set_result/set_exception.
            return await fut

        # We own the loader. Run it outside the lock.
        try:
            value = await loader()
        except BaseException as exc:
            async with self._lock:
                self._pending.pop(key, None)
            if not fut.done():
                fut.set_exception(exc)
            # Always retrieve the exception so asyncio doesn't log a
            # stray "Future exception was never retrieved" when no
            # other coroutine was waiting on it.
            fut.exception()
            raise

        async with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, value)
            self._pending.pop(key, None)
        if not fut.done():
            fut.set_result(value)
        return value

    def invalidate(self, key: str) -> None:
        """Drop *key* from the cache (best effort)."""
        self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        """Drop every key starting with *prefix*."""
        # Materialize to a list first — mutating during iteration is UB.
        for k in [k for k in self._store if k.startswith(prefix)]:
            self._store.pop(k, None)

    def clear(self) -> None:
        """Drop all cached entries. Primarily for tests."""
        self._store.clear()


# Module-level singleton used by the API layer.
default_cache: TTLCache = TTLCache(MAX_TTL_SECONDS)
