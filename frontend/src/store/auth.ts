import { defineStore } from 'pinia';
import * as authApi from '../api/auth';
import type { LoginIn, RegisterIn, RegisterOut, User } from '../types';

// localStorage keys are namespaced with `em_` so they don't collide with
// other apps that may share the origin in dev.
const LS_TOKEN_KEY = 'argus.access_token';
const LS_EXPIRES_KEY = 'em_expires_at';
const LS_USER_KEY = 'em_user';

interface AuthState {
  currentUser: User | null;
  accessToken: string | null;
  /** epoch ms when the JWT expires; null if no token */
  expiresAt: number | null;
  /** set while fetchMe / bootstrap is in flight */
  bootstrapping: boolean;
  /** window.setTimeout handle for the auto-refresh alarm */
  _refreshTimer: number | null;
}

function readStoredUser(): User | null {
  try {
    const raw = localStorage.getItem(LS_USER_KEY);
    return raw ? (JSON.parse(raw) as User) : null;
  } catch {
    return null;
  }
}

function readStoredExpiresAt(): number | null {
  const raw = localStorage.getItem(LS_EXPIRES_KEY);
  if (!raw) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({
    currentUser: readStoredUser(),
    accessToken: localStorage.getItem(LS_TOKEN_KEY),
    expiresAt: readStoredExpiresAt(),
    bootstrapping: false,
    _refreshTimer: null,
  }),

  getters: {
    isAuthenticated: (s): boolean => {
      if (!s.accessToken) return false;
      if (s.expiresAt !== null && Date.now() >= s.expiresAt) return false;
      return true;
    },
    isAdmin: (s): boolean => !!s.currentUser?.is_admin,
    isEmailVerified: (s): boolean => !!s.currentUser?.email_verified,
    /** Seconds until token expiry; -1 if no token or indeterminate. */
    secondsUntilExpiry: (s): number => {
      if (s.expiresAt === null) return -1;
      return Math.max(0, Math.floor((s.expiresAt - Date.now()) / 1000));
    },
  },

  actions: {
    /**
     * Called once from main.ts. If a persisted token looks valid, hit /auth/me to
     * confirm the server still accepts it (and pick up fresh user fields like
     * email_verified). On 401 we silently clear — the router guard will kick the
     * user to /login when they navigate to a protected route.
     */
    async bootstrap(): Promise<void> {
      if (!this.accessToken) return;
      if (this.expiresAt !== null && Date.now() >= this.expiresAt) {
        this.clearSession();
        return;
      }
      this.bootstrapping = true;
      try {
        const me = await authApi.getMe();
        this.currentUser = me;
        this._persistUser();
        this._scheduleRefresh();
      } catch {
        // token rejected — scrub state
        this.clearSession();
      } finally {
        this.bootstrapping = false;
      }
    },

    async register(body: RegisterIn): Promise<RegisterOut> {
      return authApi.register(body);
    },

    async login(body: LoginIn): Promise<User> {
      const resp = await authApi.login(body);
      this.setToken(resp.access_token, resp.expires_in);
      this.currentUser = resp.user;
      this._persistUser();
      this._scheduleRefresh();
      return resp.user;
    },

    async logout(): Promise<void> {
      // Fire and forget — backend is idempotent, we don't want network issues
      // to prevent a local sign-out.
      try {
        await authApi.logout();
      } catch {
        // ignore
      }
      this.clearSession();
    },

    async refresh(): Promise<void> {
      if (!this.accessToken) return;
      try {
        const resp = await authApi.refresh();
        this.setToken(resp.access_token, resp.expires_in);
        this._scheduleRefresh();
      } catch {
        // refresh failed — let interceptor's 401 path handle it if that was the cause
      }
    },

    async fetchMe(): Promise<User | null> {
      if (!this.accessToken) return null;
      try {
        const me = await authApi.getMe();
        this.currentUser = me;
        this._persistUser();
        return me;
      } catch {
        return null;
      }
    },

    /**
     * Like fetchMe but throws on network/auth failure so callers (e.g. the
     * router guard) can distinguish "token confirmed valid" from "token
     * rejected by server". The router guard races this against a 3-second
     * timeout and calls clearSession() if it rejects, ensuring isAuthenticated
     * is correct before the routing decision is made.
     */
    async validateSession(): Promise<User> {
      const me = await authApi.getMe();
      this.currentUser = me;
      this._persistUser();
      return me;
    },

    setToken(token: string, expiresIn: number): void {
      this.accessToken = token;
      this.expiresAt = Date.now() + expiresIn * 1000;
      localStorage.setItem(LS_TOKEN_KEY, token);
      localStorage.setItem(LS_EXPIRES_KEY, String(this.expiresAt));
    },

    clearSession(): void {
      this.accessToken = null;
      this.expiresAt = null;
      this.currentUser = null;
      localStorage.removeItem(LS_TOKEN_KEY);
      localStorage.removeItem(LS_EXPIRES_KEY);
      localStorage.removeItem(LS_USER_KEY);
      this._cancelRefresh();
    },

    /**
     * Refresh the JWT 30 minutes before expiry. For short tokens (under 30 min)
     * refresh halfway through. Reschedules itself after each successful refresh
     * via the `refresh()` action.
     */
    _scheduleRefresh(): void {
      this._cancelRefresh();
      if (this.expiresAt === null) return;
      const msUntilExpiry = this.expiresAt - Date.now();
      if (msUntilExpiry <= 0) return;
      const thirtyMin = 30 * 60 * 1000;
      const delay =
        msUntilExpiry > thirtyMin
          ? msUntilExpiry - thirtyMin
          : Math.max(5_000, Math.floor(msUntilExpiry / 2));
      this._refreshTimer = window.setTimeout(() => {
        void this.refresh();
      }, delay);
    },

    _cancelRefresh(): void {
      if (this._refreshTimer !== null) {
        window.clearTimeout(this._refreshTimer);
        this._refreshTimer = null;
      }
    },

    _persistUser(): void {
      if (this.currentUser) {
        try {
          localStorage.setItem(LS_USER_KEY, JSON.stringify(this.currentUser));
        } catch {
          // ignore quota errors
        }
      }
    },
  },
});
