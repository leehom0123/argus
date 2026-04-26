import { defineStore } from 'pinia';
import { getDashboard } from '../api/dashboard';
import type { DashboardData } from '../types';

type Scope = 'mine' | 'shared' | 'all';

interface DashState {
  data: DashboardData | null;
  scope: Scope;
  loading: boolean;
  autoRefresh: boolean;
  refreshSec: number;
  lastFetchedAt: number | null;
  error: string | null;
  _timer: number | null;
}

const STORAGE_KEY = 'argus.dashboard';

function loadStored(): Partial<DashState> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Partial<DashState>) : {};
  } catch {
    return {};
  }
}

export const useDashboardStore = defineStore('dashboard', {
  state: (): DashState => ({
    data: null,
    scope: 'mine',
    loading: false,
    autoRefresh: true,
    refreshSec: 10,
    lastFetchedAt: null,
    error: null,
    _timer: null,
    ...loadStored(),
  }),
  getters: {
    counters: (s) => s.data?.counters ?? {},
    projects: (s) => s.data?.projects ?? [],
    activity: (s) => s.data?.activity ?? [],
    hosts: (s) => s.data?.hosts ?? [],
    notifications: (s) => s.data?.notifications ?? [],
  },
  actions: {
    async fetch(): Promise<void> {
      this.loading = true;
      this.error = null;
      try {
        this.data = await getDashboard({ scope: this.scope });
        this.lastFetchedAt = Date.now();
      } catch (e) {
        this.error = (e as Error).message;
      } finally {
        this.loading = false;
      }
    },
    setScope(scope: Scope): void {
      if (this.scope === scope) return;
      this.scope = scope;
      this._persist();
      void this.fetch();
    },
    setAutoRefresh(on: boolean): void {
      this.autoRefresh = on;
      this._persist();
      this._applyTimer();
    },
    setRefreshSec(sec: number): void {
      this.refreshSec = Math.max(5, sec);
      this._persist();
      this._applyTimer();
    },
    start(): void {
      void this.fetch();
      this._applyTimer();
    },
    stop(): void {
      if (this._timer !== null) {
        window.clearInterval(this._timer);
        this._timer = null;
      }
    },
    _applyTimer(): void {
      this.stop();
      if (!this.autoRefresh) return;
      this._timer = window.setInterval(
        () => void this.fetch(),
        Math.max(5, this.refreshSec) * 1000,
      );
    },
    _persist(): void {
      try {
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({
            scope: this.scope,
            autoRefresh: this.autoRefresh,
            refreshSec: this.refreshSec,
          }),
        );
      } catch {
        // ignore quota
      }
    },
  },
});
