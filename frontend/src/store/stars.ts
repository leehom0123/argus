import { defineStore } from 'pinia';
import { addStar, listStars, removeStar } from '../api/stars';
import type { Star } from '../types';

interface StarsState {
  items: Star[];
  loading: boolean;
  loaded: boolean;
  error: string | null;
}

function keyOf(targetType: 'project' | 'batch', targetId: string): string {
  return `${targetType}::${targetId}`;
}

export const useStarsStore = defineStore('stars', {
  state: (): StarsState => ({
    items: [],
    loading: false,
    loaded: false,
    error: null,
  }),
  getters: {
    /** Membership Set for O(1) checks in list views. */
    index(state): Set<string> {
      return new Set(state.items.map((s) => keyOf(s.target_type, s.target_id)));
    },
    starredProjects(state): Set<string> {
      return new Set(
        state.items.filter((s) => s.target_type === 'project').map((s) => s.target_id),
      );
    },
    starredBatches(state): Set<string> {
      return new Set(
        state.items.filter((s) => s.target_type === 'batch').map((s) => s.target_id),
      );
    },
  },
  actions: {
    isStarred(targetType: 'project' | 'batch', targetId: string): boolean {
      return this.index.has(keyOf(targetType, targetId));
    },
    async fetch(): Promise<void> {
      this.loading = true;
      this.error = null;
      try {
        this.items = (await listStars()) ?? [];
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
    async toggle(targetType: 'project' | 'batch', targetId: string): Promise<boolean> {
      const currentlyStarred = this.isStarred(targetType, targetId);
      if (currentlyStarred) {
        // Optimistic remove.
        const before = this.items;
        this.items = this.items.filter(
          (s) => !(s.target_type === targetType && s.target_id === targetId),
        );
        try {
          await removeStar(targetType, targetId);
          return false;
        } catch (e) {
          this.items = before;
          this.error = (e as Error).message;
          throw e;
        }
      } else {
        const optimistic: Star = {
          target_type: targetType,
          target_id: targetId,
          starred_at: new Date().toISOString(),
        };
        this.items = [optimistic, ...this.items];
        try {
          const real = await addStar({ target_type: targetType, target_id: targetId });
          this.items = [real, ...this.items.filter((s) => s !== optimistic)];
          return true;
        } catch (e) {
          this.items = this.items.filter((s) => s !== optimistic);
          this.error = (e as Error).message;
          throw e;
        }
      }
    },
  },
});
