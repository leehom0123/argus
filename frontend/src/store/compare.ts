// Compare-pool store. The selection in here mirrors Pins when a user
// navigates to /compare without an explicit query string, but the two are
// decoupled so a user can compare an ad-hoc set without polluting pins.

import { defineStore } from 'pinia';
import { getCompare } from '../api/compare';
import type { CompareData } from '../types';

/**
 * Maximum batches that can be compared side-by-side. Mirrors the backend's
 * MAX_COMPARE_BATCHES (backend/schemas/compare.py); bumping one without
 * the other would have the UI happily POST 32 ids for the server to 400.
 *
 * Deliberately separate from PIN_LIMIT (store/pins.ts): pins is a "quick
 * favourites" shortlist surfaced in the sidebar badge — keeping that at 4
 * avoids crowding the nav — whereas compare is a one-off analysis pool
 * populated from ?batches=..., the Projects page, or sweep scripts.
 */
export const COMPARE_LIMIT = 32;

interface CompareState {
  selection: string[];
  data: CompareData | null;
  loading: boolean;
  error: string | null;
}

export const useCompareStore = defineStore('compare', {
  state: (): CompareState => ({
    selection: [],
    data: null,
    loading: false,
    error: null,
  }),
  getters: {
    count: (s) => s.selection.length,
    canFetch: (s) => s.selection.length >= 2 && s.selection.length <= COMPARE_LIMIT,
  },
  actions: {
    setSelection(ids: string[]): void {
      // De-dupe and cap to COMPARE_LIMIT.
      const seen = new Set<string>();
      const cleaned: string[] = [];
      for (const id of ids) {
        if (!id || seen.has(id)) continue;
        seen.add(id);
        cleaned.push(id);
        if (cleaned.length >= COMPARE_LIMIT) break;
      }
      this.selection = cleaned;
    },
    add(id: string): boolean {
      if (this.selection.includes(id)) return true;
      if (this.selection.length >= COMPARE_LIMIT) return false;
      this.selection = [...this.selection, id];
      return true;
    },
    remove(id: string): void {
      this.selection = this.selection.filter((b) => b !== id);
    },
    clear(): void {
      this.selection = [];
      this.data = null;
    },
    async fetch(): Promise<void> {
      if (!this.canFetch) {
        this.data = null;
        return;
      }
      this.loading = true;
      this.error = null;
      try {
        this.data = await getCompare(this.selection);
      } catch (e) {
        this.error = (e as Error).message;
      } finally {
        this.loading = false;
      }
    },
  },
});
