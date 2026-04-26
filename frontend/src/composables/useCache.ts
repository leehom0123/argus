/**
 * useCache — a module-scope, in-memory TTL cache with request deduplication.
 *
 * Scope: solves the "switching pages → watching cards pop in" UX problem for
 * read-only endpoints that don't change often within a session (e.g. the
 * user profile, the empty-state hint catalog, the project / host lists).
 *
 * Design choices:
 *
 *   - Module-scope Map, not a Pinia store. We want the cache to outlive the
 *     current route's component tree so a fast back-forth between
 *     ProjectList → ProjectDetail → ProjectList renders the list instantly
 *     on the second visit. A Pinia store would work too but the overhead
 *     (reactivity, devtools) is unneeded here — consumers just call
 *     ``cached(key, loader)`` and get back a Promise.
 *
 *   - TTL per entry (passed to ``cached()``), not a global default. Different
 *     endpoints have different freshness requirements (hints never change
 *     in a session; projects change when a new batch registers).
 *
 *   - In-flight dedup: if two components both call cached() for the same key
 *     simultaneously, only one network request fires and both receive the
 *     same Promise.
 *
 *   - Opt-out: explicit ``invalidate(key)`` / ``clearCache()`` for refresh
 *     buttons. Writes never go through the cache — only reads.
 *
 *   - Stale-while-revalidate: ``peek(key)`` returns the cached value
 *     synchronously (possibly stale) without triggering a network fetch.
 *     Used for page-transition previews — render what we have immediately,
 *     let the full fetch settle in the background.
 */

interface CacheEntry<T> {
  /** Resolved value (null until the first fetch resolves). */
  value: T | null;
  /** epoch ms the value was written (loader resolution time). */
  writtenAt: number;
  /** TTL in ms, snapshot at write time. */
  ttlMs: number;
  /** The current in-flight promise, if any — dedups concurrent callers. */
  inflight: Promise<T> | null;
}

const store = new Map<string, CacheEntry<unknown>>();

/**
 * Get-or-load. If a fresh entry (age < ttlMs) exists, return it; else kick
 * off ``loader()`` and cache the result. Concurrent callers share the same
 * in-flight promise.
 *
 * ``loader`` is only invoked on a miss — pass a closure, not a pre-computed
 * promise, or you'll eagerly fetch on every call.
 */
export async function cached<T>(
  key: string,
  loader: () => Promise<T>,
  ttlMs = 30_000,
): Promise<T> {
  const existing = store.get(key) as CacheEntry<T> | undefined;
  const now = Date.now();

  // Fresh cached value — return synchronously wrapped in a Promise.
  if (existing && existing.value !== null && now - existing.writtenAt < existing.ttlMs) {
    return existing.value;
  }

  // Already fetching — piggy-back on the in-flight promise.
  if (existing?.inflight) {
    return existing.inflight;
  }

  const entry: CacheEntry<T> = existing ?? {
    value: null,
    writtenAt: 0,
    ttlMs,
    inflight: null,
  };

  const promise = loader()
    .then((v) => {
      entry.value = v;
      entry.writtenAt = Date.now();
      entry.ttlMs = ttlMs;
      entry.inflight = null;
      return v;
    })
    .catch((err) => {
      // Don't poison the cache with a failure — drop the in-flight marker
      // so the next caller can retry, but keep the (possibly-stale) value
      // so UIs still have something to render on transient errors.
      entry.inflight = null;
      throw err;
    });

  entry.inflight = promise;
  store.set(key, entry as CacheEntry<unknown>);
  return promise;
}

/**
 * Synchronously return the current cached value for ``key``, regardless of
 * freshness. Returns null when the key has never been fetched.
 *
 * Used for stale-while-revalidate UI — render ``peek(key)`` immediately,
 * then call ``cached(key, loader)`` to settle the real fetch.
 */
export function peek<T>(key: string): T | null {
  const entry = store.get(key) as CacheEntry<T> | undefined;
  return entry?.value ?? null;
}

/**
 * Invalidate a single key (or every key matching a prefix). The next
 * cached() call for that key will re-fetch.
 *
 * Refresh buttons should call ``invalidate()`` before ``cached()`` so
 * clicking "Refresh" actually goes to the network.
 */
export function invalidate(keyOrPrefix: string, prefix = false): void {
  if (!prefix) {
    store.delete(keyOrPrefix);
    return;
  }
  for (const k of store.keys()) {
    if (k.startsWith(keyOrPrefix)) store.delete(k);
  }
}

/** Nuke the whole cache. Currently used by auth.clearSession(). */
export function clearCache(): void {
  store.clear();
}

// ---------------------------------------------------------------------------
// Prefetch on hover
// ---------------------------------------------------------------------------

const PREFETCH_DELAY_MS = 100;
const prefetchTimers = new Map<string, number>();

/**
 * Schedule a prefetch for ``key`` after a short debounce (100 ms). Used on
 * ``@mouseenter`` for list rows / cards: if the mouse lingers long enough to
 * suggest real intent, we warm the cache so the subsequent click feels
 * instant. Fast mouse fly-bys trigger cancelPrefetch() from
 * ``@mouseleave`` and no request fires.
 *
 * Safe to call repeatedly with the same key — redundant schedules are
 * collapsed.
 */
export function schedulePrefetch<T>(
  key: string,
  loader: () => Promise<T>,
  ttlMs = 30_000,
): void {
  // Already fresh? Skip.
  const entry = store.get(key);
  if (entry && entry.value !== null && Date.now() - entry.writtenAt < entry.ttlMs) {
    return;
  }
  // Already scheduled? Skip (don't reset — fire on the original schedule).
  if (prefetchTimers.has(key)) return;

  const handle = window.setTimeout(() => {
    prefetchTimers.delete(key);
    void cached(key, loader, ttlMs).catch(() => {
      // Swallow — prefetch failures shouldn't surface. The later click will
      // retry and the interceptor's normal 4xx handling kicks in then.
    });
  }, PREFETCH_DELAY_MS);
  prefetchTimers.set(key, handle);
}

/** Cancel a pending prefetch scheduled via schedulePrefetch(). */
export function cancelPrefetch(key: string): void {
  const handle = prefetchTimers.get(key);
  if (handle !== undefined) {
    window.clearTimeout(handle);
    prefetchTimers.delete(key);
  }
}

// ---------------------------------------------------------------------------
// Canonical cache-key builders — one source of truth so prefetch + fetch
// agree on the key string. Keep these adjacent to the endpoints they serve.
// ---------------------------------------------------------------------------

export const cacheKey = {
  metaHints: (): string => 'meta:hints',
  me: (): string => 'auth:me',
  hosts: (): string => 'hosts:list',
  projects: (scope: string): string => `projects:${scope}`,
  projectSummary: (project: string): string => `project:${project}`,
  batchSummary: (batchId: string): string => `batch:${batchId}`,
};

/** Suggested TTLs, in ms. Policy cap: **10 seconds max** for anything that
 * surfaces live backend state to the user — per PM directive 2026-04-25.
 * Static catalogs (i18n hints, user profile) get longer TTLs because their
 * "staleness" isn't user-visible between navigations.
 */
export const cacheTtl = {
  /** Hints are static for the session — the backend ships an immutable catalog. */
  metaHints: 10 * 60_000, // 10 min (static — not subject to live-data cap)
  /** User profile changes rarely; 5 min matches the JWT refresh cadence. */
  me: 5 * 60_000, // 5 min (static — not subject to live-data cap)
  /** Host list: was 30s, now capped at 10s per policy. */
  hosts: 10_000,
  /** Project list: was 30s, now capped at 10s per policy. */
  projects: 10_000,
  /** Individual project/batch summaries for stale-while-revalidate. */
  summary: 10_000,
};
