/**
 * guard.test.ts
 *
 * Unit tests for the beforeEach navigation guard in src/router/index.ts.
 *
 * Strategy
 * --------
 * We do NOT import the real router (it would register live lazy-loaded
 * components and kick off Hydra on every test).  Instead we extract the
 * guard logic into a testable factory: the guard is the *only* export under
 * test here, exercised through a hand-crafted `beforeEach` invocation.
 *
 * Auth store: mocked with vi.mock so each test controls exactly which
 * properties/actions are visible.
 *
 * router.hasRoute: injected via the module-level mock so we can flip the
 * "demo route present" flag per test.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { RouteLocationNormalized } from 'vue-router';

// ---------------------------------------------------------------------------
// Mock the auth store module before the router module imports it.
// ---------------------------------------------------------------------------

// Shared mutable state the mock store reads from:
const mockAuthState = {
  accessToken: null as string | null,
  currentUser: null as object | null,
  isAuthenticated: false,
  isAdmin: false,
  validateSessionImpl: async () => {
    throw new Error('default: validateSession not configured');
  },
};

vi.mock('../../store/auth', () => ({
  useAuthStore: () => ({
    get accessToken() { return mockAuthState.accessToken; },
    get currentUser() { return mockAuthState.currentUser; },
    get isAuthenticated() { return mockAuthState.isAuthenticated; },
    get isAdmin() { return mockAuthState.isAdmin; },
    validateSession: () => mockAuthState.validateSessionImpl(),
    clearSession: vi.fn(() => {
      mockAuthState.accessToken = null;
      mockAuthState.currentUser = null;
      mockAuthState.isAuthenticated = false;
    }),
  }),
}));

// ---------------------------------------------------------------------------
// Mock ant-design-vue's notification (used by admin guard).
// ---------------------------------------------------------------------------
vi.mock('ant-design-vue', () => ({
  notification: { warning: vi.fn() },
}));

// ---------------------------------------------------------------------------
// Mock vue-router — only the pieces the guard touches.
// We capture the beforeEach callback so we can call it directly in tests.
// ---------------------------------------------------------------------------
type GuardFn = (to: RouteLocationNormalized) => unknown;
let capturedGuard: GuardFn | null = null;
let mockHasRoute = false;

vi.mock('vue-router', () => {
  const router = {
    beforeEach: (fn: GuardFn) => {
      capturedGuard = fn;
    },
    hasRoute: (name: string) => {
      if (name === 'public-projects') return mockHasRoute;
      return false;
    },
  };
  return {
    createRouter: () => router,
    createWebHistory: () => ({}),
  };
});

// ---------------------------------------------------------------------------
// Helper to build a minimal RouteLocationNormalized.
// ---------------------------------------------------------------------------
function makeRoute(
  overrides: {
    path?: string;
    fullPath?: string;
    name?: string;
    requiresAuth?: boolean;
    requiresAdmin?: boolean;
    isPublic?: boolean;
  } = {},
): RouteLocationNormalized {
  const {
    path = '/',
    fullPath,
    name = '',
    requiresAuth = false,
    requiresAdmin = false,
    isPublic = false,
  } = overrides;
  return {
    path,
    fullPath: fullPath ?? path,
    name,
    params: {},
    query: {},
    hash: '',
    matched: [
      {
        meta: {
          ...(requiresAuth ? { requiresAuth: true } : {}),
          ...(requiresAdmin ? { requiresAdmin: true } : {}),
          ...(isPublic ? { public: true } : {}),
        },
      } as unknown as RouteLocationNormalized['matched'][number],
    ],
    meta: {},
    redirectedFrom: undefined,
  } as unknown as RouteLocationNormalized;
}

// ---------------------------------------------------------------------------
// Import the router module so the guard is registered via the mock above.
// ---------------------------------------------------------------------------
// Dynamic import is required so the mocks above take effect first.
beforeEach(async () => {
  // Reset shared state for every test.
  mockAuthState.accessToken = null;
  mockAuthState.currentUser = null;
  mockAuthState.isAuthenticated = false;
  mockAuthState.isAdmin = false;
  mockHasRoute = false;
  capturedGuard = null;

  // Re-import the router module so beforeEach re-registers the guard.
  vi.resetModules();
  await import('../index');
});

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Convenience wrapper that calls the guard and returns the navigation result.
// ---------------------------------------------------------------------------
async function runGuard(to: RouteLocationNormalized): Promise<unknown> {
  if (!capturedGuard) throw new Error('guard was not captured — check the mock');
  return capturedGuard(to);
}

// ===========================================================================
// Tests
// ===========================================================================

describe('router guard', () => {
  // -------------------------------------------------------------------------
  // 1. Unauth visitor on / → /demo when demo route present
  // -------------------------------------------------------------------------
  it('redirects unauth visitor on / to /demo when public-projects route exists', async () => {
    mockHasRoute = true;
    // No token → validateSession never called → stays unauth
    const result = await runGuard(
      makeRoute({ path: '/', fullPath: '/', name: 'Dashboard', requiresAuth: true }),
    );
    expect(result).toEqual({ path: '/demo' });
  });

  // -------------------------------------------------------------------------
  // 2. Unauth visitor on / → /login when demo route absent
  // -------------------------------------------------------------------------
  it('redirects unauth visitor on / to /login when demo route is absent', async () => {
    mockHasRoute = false;
    const result = await runGuard(
      makeRoute({ path: '/', fullPath: '/', name: 'Dashboard', requiresAuth: true }),
    );
    // No redirect query param for the root path
    expect(result).toEqual({ path: '/login', query: undefined });
  });

  // -------------------------------------------------------------------------
  // 3. Authenticated user on /login → /
  // -------------------------------------------------------------------------
  it('bounces an authenticated user away from /login to /', async () => {
    mockAuthState.accessToken = 'valid-token';
    mockAuthState.currentUser = { id: 1, email: 'test@example.com' };
    mockAuthState.isAuthenticated = true;
    // validateSession succeeds (user is already set so it won't be called)
    mockAuthState.validateSessionImpl = async () => mockAuthState.currentUser as never;

    const result = await runGuard(
      makeRoute({ path: '/login', fullPath: '/login', name: 'Login', isPublic: true }),
    );
    expect(result).toEqual({ path: '/' });
  });

  // -------------------------------------------------------------------------
  // 4. Expired token + fetchMe (validateSession) rejects with 401 → /demo
  // -------------------------------------------------------------------------
  it('clears session and redirects to /demo when validateSession rejects (expired token)', async () => {
    mockHasRoute = true;
    // Token present but user null → guard will call validateSession
    mockAuthState.accessToken = 'expired-token';
    mockAuthState.currentUser = null;
    mockAuthState.isAuthenticated = true; // getter initially says "yes" (hasn't been cleared yet)

    mockAuthState.validateSessionImpl = async () => {
      throw Object.assign(new Error('Unauthorized'), { response: { status: 401 } });
    };

    const result = await runGuard(
      makeRoute({ path: '/', fullPath: '/', name: 'Dashboard', requiresAuth: true }),
    );

    // After clearSession is called, isAuthenticated becomes false → redirect to demo
    expect(result).toEqual({ path: '/demo' });
  });

  // -------------------------------------------------------------------------
  // 5. Protected route /batches/foo, unauth + demo absent → /login?redirect=...
  // -------------------------------------------------------------------------
  it('redirects unauth user on /batches/foo to /login?redirect=... when demo absent', async () => {
    mockHasRoute = false;
    const result = await runGuard(
      makeRoute({
        path: '/batches/foo',
        fullPath: '/batches/foo',
        name: 'BatchDetail',
        requiresAuth: true,
      }),
    );
    expect(result).toEqual({ path: '/login', query: { redirect: '/batches/foo' } });
  });

  // -------------------------------------------------------------------------
  // 6. Protected route /batches/foo, unauth + demo present → /demo (no redirect)
  // -------------------------------------------------------------------------
  it('redirects unauth user on /batches/foo to /demo (no redirect param) when demo present', async () => {
    mockHasRoute = true;
    const result = await runGuard(
      makeRoute({
        path: '/batches/foo',
        fullPath: '/batches/foo',
        name: 'BatchDetail',
        requiresAuth: true,
      }),
    );
    // Unauth visitors on /demo don't need a ?redirect= nudge toward login
    expect(result).toEqual({ path: '/demo' });
  });
});
