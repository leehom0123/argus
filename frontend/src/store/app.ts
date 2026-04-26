import { defineStore } from 'pinia';

interface AppState {
  darkMode: boolean;
  autoRefreshSec: number; // default list-page refresh interval
  siderCollapsed: boolean;
}

const STORAGE_KEY = 'argus.ui';

function loadStored(): Partial<AppState> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Partial<AppState>) : {};
  } catch {
    return {};
  }
}

/** Sync the `dark` class on <html> so global CSS can react to theme changes. */
function applyDarkClass(dark: boolean): void {
  if (dark) {
    document.documentElement.classList.add('dark');
  } else {
    document.documentElement.classList.remove('dark');
  }
}

export const useAppStore = defineStore('app', {
  state: (): AppState => ({
    darkMode: true,
    autoRefreshSec: 10,
    siderCollapsed: false,
    ...loadStored(),
  }),
  actions: {
    toggleDark() {
      this.darkMode = !this.darkMode;
      applyDarkClass(this.darkMode);
      this.persist();
    },
    /** Call once at app startup to apply the persisted preference immediately. */
    applyStoredDark() {
      applyDarkClass(this.darkMode);
    },
    setAutoRefresh(sec: number) {
      this.autoRefreshSec = Math.max(0, sec);
      this.persist();
    },
    setCollapsed(v: boolean) {
      this.siderCollapsed = v;
      this.persist();
    },
    persist() {
      try {
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({
            darkMode: this.darkMode,
            autoRefreshSec: this.autoRefreshSec,
            siderCollapsed: this.siderCollapsed,
          }),
        );
      } catch {
        // ignore — private browsing / quota
      }
    },
  },
});
