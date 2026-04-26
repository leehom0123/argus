import { defineStore } from 'pinia';
import { notification } from 'ant-design-vue';
import { addPin, listPins, removePin } from '../api/pins';
import type { Pin } from '../types';

/** UI hard-cap per requirements §16.7 — backend enforces the same limit. */
export const PIN_LIMIT = 4;

interface PinsState {
  items: Pin[];
  loading: boolean;
  loaded: boolean;
  error: string | null;
}

export const usePinsStore = defineStore('pins', {
  state: (): PinsState => ({
    items: [],
    loading: false,
    loaded: false,
    error: null,
  }),
  getters: {
    ids(state): string[] {
      return state.items.map((p) => p.batch_id);
    },
    index(state): Set<string> {
      return new Set(state.items.map((p) => p.batch_id));
    },
    count(state): number {
      return state.items.length;
    },
    isFull(state): boolean {
      return state.items.length >= PIN_LIMIT;
    },
  },
  actions: {
    isPinned(batchId: string): boolean {
      return this.index.has(batchId);
    },
    async fetch(): Promise<void> {
      this.loading = true;
      this.error = null;
      try {
        this.items = (await listPins()) ?? [];
        this.loaded = true;
      } catch (e) {
        this.error = (e as Error).message;
      } finally {
        this.loading = false;
      }
    },
    async ensureLoaded(): Promise<void> {
      if (this.loaded || this.loading) return;
      await this.fetch();
    },
    async toggle(batchId: string): Promise<boolean> {
      if (this.isPinned(batchId)) {
        // Remove.
        const before = this.items;
        this.items = this.items.filter((p) => p.batch_id !== batchId);
        try {
          await removePin(batchId);
          return false;
        } catch (e) {
          this.items = before;
          this.error = (e as Error).message;
          throw e;
        }
      }
      if (this.isFull) {
        notification.warning({
          message: `Compare pool is full (${PIN_LIMIT} max)`,
          description: 'Unpin a batch before pinning another.',
          duration: 3,
        });
        return false;
      }
      const optimistic: Pin = { batch_id: batchId, pinned_at: new Date().toISOString() };
      this.items = [...this.items, optimistic];
      try {
        const real = await addPin({ batch_id: batchId });
        this.items = [...this.items.filter((p) => p !== optimistic), real];
        return true;
      } catch (e) {
        this.items = this.items.filter((p) => p !== optimistic);
        this.error = (e as Error).message;
        throw e;
      }
    },
    async clearAll(): Promise<void> {
      const snapshot = [...this.items];
      this.items = [];
      // Best-effort: fire removes in parallel. If any fail we refetch.
      const results = await Promise.allSettled(
        snapshot.map((p) => removePin(p.batch_id)),
      );
      if (results.some((r) => r.status === 'rejected')) {
        await this.fetch();
      }
    },
  },
});
