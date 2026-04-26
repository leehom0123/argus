/**
 * One-time migration of legacy localStorage keys.
 *
 * Pre-v0.1.0 builds wrote a handful of `em_*`-prefixed keys (token, refresh
 * token, locale, theme, pinned projects). The v0.1.x cut-over moved active
 * storage onto the `argus.*` / `locale` namespaces, but old keys still linger
 * in users' browsers from earlier sessions and never get cleared. This module
 * scrubs them once on app boot and records a flag so we never run twice.
 *
 * NOTE: only truly abandoned keys are listed here. `em_user` and
 * `em_expires_at` are still in active use by `store/auth.ts` and MUST NOT be
 * removed — touching them would log every existing user out. Once those keys
 * are renamed in a future release, extend `LEGACY_KEYS` and bump the flag
 * version (e.g. `argus_storage_migrated_v2`).
 */

const LEGACY_KEYS: readonly string[] = [
  // Auth — replaced by `argus.access_token`
  'em_token',
  'em_refresh_token',
  // i18n — replaced by `locale`
  'em_locale',
  // UI prefs — replaced by `argus.ui` (darkMode lives inside the JSON blob)
  'em_theme',
  // Dashboard prefs — replaced by `argus.dashboard`
  'em_pinned_projects',
];

const MIGRATED_FLAG = 'argus_storage_migrated_v1';

/**
 * Remove abandoned `em_*` localStorage keys exactly once per browser. Safe to
 * call on every boot — the flag short-circuits subsequent invocations so we
 * don't pay the cost of repeated `removeItem` calls (and don't accidentally
 * clobber any future key that happens to share an old name).
 *
 * Must run before Pinia / router setup in `main.ts`, since auth and UI stores
 * read localStorage synchronously at construction time.
 */
export function migrateLegacyStorage(): void {
  // Bail out if a previous boot already cleaned these keys.
  if (localStorage.getItem(MIGRATED_FLAG) !== null) return;

  for (const key of LEGACY_KEYS) {
    localStorage.removeItem(key);
  }
  localStorage.setItem(MIGRATED_FLAG, '1');
}

// Exported for tests only.
export const __INTERNAL = {
  LEGACY_KEYS,
  MIGRATED_FLAG,
};
