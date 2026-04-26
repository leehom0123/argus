import { defineStore } from 'pinia';
import { getMetaHints } from '../api/client';

/**
 * Empty-state hint catalog store (roadmap #30).
 *
 * The backend's ``GET /api/meta/hints`` returns one string per variant
 * ("empty_hosts", "empty_batches", …) in the caller's Accept-Language.
 * We fetch once at app mount so every EmptyState render is synchronous.
 *
 * Failure policy: silently fall back to null — the EmptyState component
 * then renders the vue-i18n fallback key, so the UI still has readable
 * copy even if the backend is unreachable.
 */
interface HintsState {
  locale: string | null;
  hints: Record<string, string>;
  loading: boolean;
  loaded: boolean;
}

export const useHintsStore = defineStore('hints', {
  state: (): HintsState => ({
    locale: null,
    hints: {},
    loading: false,
    loaded: false,
  }),
  getters: {
    /** Lookup a hint by variant key (e.g. "empty_hosts"). Returns null if missing. */
    hint:
      (state) =>
      (variant: string): string | null =>
        state.hints[variant] ?? null,
  },
  actions: {
    /**
     * Fetch the catalog once. Safe to call on every mount — subsequent
     * calls short-circuit unless ``force`` is set.
     */
    async ensureLoaded(force = false): Promise<void> {
      if (this.loaded && !force) return;
      if (this.loading) return;
      this.loading = true;
      try {
        const res = await getMetaHints();
        this.locale = res.locale;
        this.hints = res.hints ?? {};
        this.loaded = true;
      } catch (err) {
        // Silent degradation — EmptyState falls back to the i18n key.
        // eslint-disable-next-line no-console
        console.warn('[hints] failed to load /meta/hints', err);
      } finally {
        this.loading = false;
      }
    },
  },
});
