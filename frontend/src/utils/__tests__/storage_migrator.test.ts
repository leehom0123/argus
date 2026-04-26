/**
 * storage_migrator.test.ts
 *
 * Covers the one-time legacy-key cleanup invoked from `main.ts`:
 *   1. First run on a dirty browser → all legacy keys removed, flag set.
 *   2. Second run on the same browser → idempotent (flag still present, no
 *      side-effects, no thrown errors).
 *   3. Flag pre-existing → returns immediately without touching localStorage,
 *      so a hypothetical future legacy-named key doesn't get scrubbed by an
 *      already-migrated browser.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { migrateLegacyStorage, __INTERNAL } from '../storage_migrator';

const { LEGACY_KEYS, MIGRATED_FLAG } = __INTERNAL;

describe('migrateLegacyStorage', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it('removes every legacy em_* key and sets the migrated flag on first run', () => {
    // Seed the browser with legacy values, plus an unrelated argus.* key that
    // must survive the migration.
    for (const key of LEGACY_KEYS) {
      localStorage.setItem(key, `legacy-value-for-${key}`);
    }
    localStorage.setItem('argus.access_token', 'keep-me');
    localStorage.setItem('argus.ui', '{"darkMode":true}');

    migrateLegacyStorage();

    for (const key of LEGACY_KEYS) {
      expect(localStorage.getItem(key)).toBeNull();
    }
    expect(localStorage.getItem(MIGRATED_FLAG)).toBe('1');
    // Unrelated argus.* keys must be untouched.
    expect(localStorage.getItem('argus.access_token')).toBe('keep-me');
    expect(localStorage.getItem('argus.ui')).toBe('{"darkMode":true}');
  });

  it('is idempotent on a second invocation', () => {
    // First run: sets the flag.
    migrateLegacyStorage();
    expect(localStorage.getItem(MIGRATED_FLAG)).toBe('1');

    // Second run: flag still present, legacy keys still absent, no throws.
    expect(() => migrateLegacyStorage()).not.toThrow();
    expect(localStorage.getItem(MIGRATED_FLAG)).toBe('1');
    for (const key of LEGACY_KEYS) {
      expect(localStorage.getItem(key)).toBeNull();
    }
  });

  it('returns immediately when the flag is already set without touching localStorage', () => {
    // Pre-set the flag — simulates a browser that already migrated in a
    // previous session.
    localStorage.setItem(MIGRATED_FLAG, '1');

    // Spy on removeItem to prove the early-return short-circuits the loop.
    const removeSpy = vi.spyOn(Storage.prototype, 'removeItem');
    const setSpy = vi.spyOn(Storage.prototype, 'setItem');

    migrateLegacyStorage();

    expect(removeSpy).not.toHaveBeenCalled();
    expect(setSpy).not.toHaveBeenCalled();
    expect(localStorage.getItem(MIGRATED_FLAG)).toBe('1');
  });
});
