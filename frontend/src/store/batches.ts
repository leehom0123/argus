import { defineStore } from 'pinia';
import { listBatches, getBatchesCompact, type BatchCompactItem } from '../api/client';
import type { Batch, BatchScope, BatchStatus } from '../types';

interface Filters {
  user?: string;
  project?: string;
  status?: BatchStatus;
  experiment_type?: string;
  q?: string; // free-text client-side search
  since?: string; // ISO
  until?: string; // ISO
  /** `mine` (default) | `shared` (shared with me) | `all` (admin only). */
  scope?: BatchScope;
}

interface BatchesState {
  filters: Filters;
  items: Batch[];
  /** Bulk compact items keyed by batch.id — populated by ``fetchCompact``. */
  compactByBatchId: Record<string, BatchCompactItem>;
  loading: boolean;
  error: string | null;
  lastFetchedAt: number | null;
}

export const useBatchesStore = defineStore('batches', {
  state: (): BatchesState => ({
    filters: {},
    items: [],
    compactByBatchId: {},
    loading: false,
    error: null,
    lastFetchedAt: null,
  }),
  getters: {
    filtered(state): Batch[] {
      const q = state.filters.q?.trim().toLowerCase();
      const etype = state.filters.experiment_type;
      const until = state.filters.until ? Date.parse(state.filters.until) : null;
      return state.items.filter((b) => {
        if (etype && b.experiment_type !== etype) return false;
        if (q) {
          const hay = `${b.id} ${b.project ?? ''} ${b.user ?? ''} ${b.host ?? ''}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        if (until && b.start_time) {
          const t = Date.parse(b.start_time);
          if (!Number.isNaN(t) && t > until) return false;
        }
        return true;
      });
    },
    projects(state): string[] {
      return [...new Set(state.items.map((b) => b.project).filter(Boolean))].sort();
    },
    experimentTypes(state): string[] {
      return [
        ...new Set(state.items.map((b) => b.experiment_type).filter(Boolean) as string[]),
      ].sort();
    },
  },
  actions: {
    setFilter<K extends keyof Filters>(key: K, value: Filters[K]) {
      this.filters[key] = value;
    },
    clearFilters() {
      this.filters = {};
    },
    async fetch() {
      this.loading = true;
      this.error = null;
      try {
        const items = await listBatches({
          user: this.filters.user,
          project: this.filters.project,
          status: this.filters.status,
          since: this.filters.since,
          scope: this.filters.scope,
          limit: 500,
        });
        this.items = items ?? [];
        this.lastFetchedAt = Date.now();
      } catch (e) {
        this.error = (e as Error).message;
      } finally {
        this.loading = false;
      }
    },
    /**
     * Bulk fetch batches + jobs + epochs + resources via the compact endpoint.
     *
     * Replaces the prior ``listBatches`` → N×4 per-card fan-out with one
     * round-trip. Populates both ``items`` (for existing store getters
     * like ``filtered`` / ``projects`` / ``experimentTypes``) and
     * ``compactByBatchId`` so BatchCompactCard can consume the pre-fetched
     * data via its ``compactData`` prop.
     */
    async fetchCompact() {
      this.loading = true;
      this.error = null;
      try {
        const payload = await getBatchesCompact({
          user: this.filters.user,
          project: this.filters.project,
          status: this.filters.status,
          since: this.filters.since,
          scope: this.filters.scope,
          limit: 500,
          resource_limit: 20,
        });
        const items: Batch[] = [];
        const map: Record<string, BatchCompactItem> = {};
        for (const it of payload.batches ?? []) {
          items.push(it.batch);
          map[it.batch.id] = it;
        }
        this.items = items;
        this.compactByBatchId = map;
        this.lastFetchedAt = Date.now();
      } catch (e) {
        this.error = (e as Error).message;
      } finally {
        this.loading = false;
      }
    },
  },
});
