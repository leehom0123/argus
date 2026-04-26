import {
  createRouter,
  createWebHistory,
  type RouteLocationNormalized,
  type RouteRecordRaw,
} from 'vue-router';
import { notification } from 'ant-design-vue';
import { useAuthStore } from '../store/auth';

// ------------------------------------------------------------------
// Route records.
//
// Meta flags:
//   - requiresAuth   → router guard kicks unauth'd users to /demo or /login
//   - requiresAdmin  → user.is_admin must be true
//   - layout:'auth'  → render inside AuthLayout (centered card, no sider)
//                       when absent, the default AppLayout in App.vue is used
//   - public: true   → explicitly anonymous-accessible (overrides requiresAuth)
// ------------------------------------------------------------------

const routes: RouteRecordRaw[] = [
  // Home is now the Dashboard (requirements §16.2). Router guard below
  // sends unauthenticated visitors to /demo (or /login if demo absent).
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('../pages/Dashboard.vue'),
    meta: { requiresAuth: true },
  },

  // ---- Auth pages (no sider, centered card) ----
  {
    path: '/login',
    name: 'Login',
    component: () => import('../pages/Login.vue'),
    meta: { layout: 'auth', public: true },
  },
  {
    path: '/register',
    name: 'Register',
    component: () => import('../pages/Register.vue'),
    meta: { layout: 'auth', public: true },
  },
  {
    path: '/verify-email',
    name: 'VerifyEmail',
    component: () => import('../pages/VerifyEmail.vue'),
    meta: { layout: 'auth', public: true },
  },
  {
    path: '/reset-password',
    name: 'ResetPassword',
    component: () => import('../pages/ResetPassword.vue'),
    meta: { layout: 'auth', public: true },
  },
  {
    // OAuth provider redirects land here with ``#token=...`` in the
    // fragment. Page is public (the token itself is the credential).
    path: '/login/oauth/complete',
    name: 'OAuthComplete',
    component: () => import('../pages/OAuthComplete.vue'),
    meta: { layout: 'auth', public: true },
  },

  // ---- Public-link viewer (/public/:slug) ----
  // Anonymous batch viewer reachable via a share slug. Renders the
  // full BatchDetail.vue in read-only mode — one component, one source
  // of truth. The route guard does not require auth.
  //
  // NOTE: BatchDetail expects `batchId`, not `slug`, as a prop. Backend
  // resolves the slug → batch-id server-side so the existing /batches/:id
  // endpoints continue to work. We pass slug through as batchId.
  {
    path: '/public/:slug',
    name: 'PublicBatch',
    component: () => import('../pages/BatchDetail.vue'),
    props: (route) => ({
      batchId: String(route.params.slug),
      readOnly: true,
    }),
    meta: { public: true, layout: 'public' },
  },

  // ---- Admin-controlled public-demo routes (/demo*) ----
  // Simplified "public" layout (no sider, just a top bar) so an
  // unauthenticated visitor isn't confronted with nav links they can't
  // use. The routes mount the SAME internal components as the authed
  // tree with readOnly=true — no parallel "simplified" pages to drift.
  {
    path: '/demo',
    name: 'public-projects',
    component: () => import('../pages/ProjectList.vue'),
    props: () => ({ readOnly: true }),
    meta: { public: true, layout: 'public' },
  },
  {
    path: '/demo/projects/:project',
    name: 'public-project',
    component: () => import('../pages/ProjectDetail.vue'),
    props: (route) => ({
      project: String(route.params.project),
      readOnly: true,
    }),
    meta: { public: true, layout: 'public' },
  },
  {
    path: '/demo/batches/:batchId',
    name: 'public-batch',
    component: () => import('../pages/BatchDetail.vue'),
    props: (route) => ({
      batchId: String(route.params.batchId),
      readOnly: true,
    }),
    meta: { public: true, layout: 'public' },
  },
  {
    path: '/demo/batches/:batchId/jobs/:jobId',
    name: 'public-job',
    component: () => import('../pages/JobDetail.vue'),
    props: (route) => ({
      batchId: String(route.params.batchId),
      jobId: String(route.params.jobId),
      readOnly: true,
    }),
    meta: { public: true, layout: 'public' },
  },

  // ---- App pages (require login) ----
  {
    path: '/batches',
    name: 'BatchList',
    component: () => import('../pages/BatchList.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/batches/:batchId',
    name: 'BatchDetail',
    component: () => import('../pages/BatchDetail.vue'),
    props: true,
    meta: { requiresAuth: true },
  },
  {
    path: '/batches/:batchId/jobs/:jobId',
    name: 'JobDetail',
    component: () => import('../pages/JobDetail.vue'),
    props: true,
    meta: { requiresAuth: true },
  },
  // Global cross-batch jobs list (#118). Filters bind to query string so
  // the Dashboard tiles can deep-link to a pre-filtered view.
  {
    path: '/jobs',
    name: 'JobsList',
    component: () => import('../pages/JobsList.vue'),
    meta: { requiresAuth: true },
  },
  // ---- Projects (Dashboard IA §16.1) ----
  {
    path: '/projects',
    name: 'ProjectList',
    component: () => import('../pages/ProjectList.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/projects/:project',
    name: 'ProjectDetail',
    component: () => import('../pages/ProjectDetail.vue'),
    props: true,
    meta: { requiresAuth: true },
  },
  {
    // Also support the canonical nested path — redirect to the existing
    // /batches/:id route so BatchDetail keeps its single implementation.
    path: '/projects/:project/batches/:batchId',
    redirect: (to) => `/batches/${encodeURIComponent(String(to.params.batchId))}`,
  },
  {
    path: '/projects/:project/batches/:batchId/jobs/:jobId',
    redirect: (to) =>
      `/batches/${encodeURIComponent(String(to.params.batchId))}/jobs/${encodeURIComponent(String(to.params.jobId))}`,
  },
  // ---- Studies (Optuna multirun, v0.2 hyperopt-ui) ----
  // Sits next to ``BatchList`` because conceptually a study is the
  // multirun-level grouping of batches. Sidebar nav surfaces this
  // under "Studies" using the ``nav.studies`` i18n key.
  {
    path: '/studies',
    name: 'StudyList',
    component: () => import('../pages/StudyList.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/studies/:name',
    name: 'StudyDetail',
    component: () => import('../pages/StudyDetail.vue'),
    props: true,
    meta: { requiresAuth: true },
  },
  // ---- Compare pool (Dashboard IA §16.7) ----
  {
    path: '/compare',
    name: 'Compare',
    component: () => import('../pages/Compare.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/hosts',
    name: 'HostList',
    component: () => import('../pages/HostList.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/hosts/:host',
    name: 'HostDetail',
    component: () => import('../pages/HostDetail.vue'),
    props: true,
    meta: { requiresAuth: true },
  },

  // ---- Settings (sub-routes) ----
  {
    path: '/settings',
    redirect: '/settings/profile',
    meta: { requiresAuth: true },
  },
  {
    path: '/settings/profile',
    name: 'SettingsProfile',
    component: () => import('../pages/settings/Profile.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings/tokens',
    name: 'SettingsTokens',
    component: () => import('../pages/settings/Tokens.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings/shares',
    name: 'SettingsShares',
    component: () => import('../pages/settings/Shares.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings/preferences',
    name: 'SettingsPreferences',
    component: () => import('../pages/settings/Preferences.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings/sessions',
    name: 'SettingsSessions',
    component: () => import('../pages/settings/Sessions.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings/password',
    name: 'SettingsPassword',
    component: () => import('../pages/settings/Password.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings/about',
    name: 'SettingsAbout',
    component: () => import('../pages/settings/About.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings/notifications',
    name: 'SettingsNotifications',
    component: () => import('../pages/settings/Notifications.vue'),
    meta: { requiresAuth: true },
  },

  // ---- One-shot unsubscribe link (token in query string, public) ----
  {
    path: '/unsubscribe',
    name: 'Unsubscribe',
    component: () => import('../pages/Unsubscribe.vue'),
    meta: { layout: 'auth', public: true },
  },

  // ---- Settings → Admin sub-pages (v0.1.4: DB-driven runtime config) ----
  // The five DB-backed forms below replace the env-only knobs they
  // wrap. Old /admin/* URLs redirect to the matching /settings/*
  // route so existing bookmarks keep working through the transition.
  {
    path: '/settings/oauth-github',
    name: 'SettingsOAuthGithub',
    component: () => import('../pages/settings/admin/OAuthGithub.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/settings/smtp',
    name: 'SettingsSmtp',
    component: () => import('../pages/settings/admin/Smtp.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/settings/retention',
    name: 'SettingsRetention',
    component: () => import('../pages/settings/admin/Retention.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/settings/feature-flags',
    name: 'SettingsFeatureFlags',
    component: () => import('../pages/settings/admin/FeatureFlags.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/settings/demo-project',
    name: 'SettingsDemoProject',
    component: () => import('../pages/settings/admin/DemoProject.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/settings/security',
    name: 'SettingsSecurity',
    component: () => import('../pages/settings/admin/Security.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },

  // ---- Admin-only pages ----
  {
    path: '/admin/users',
    name: 'AdminUsers',
    component: () => import('../pages/admin/Users.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    // Legacy alias — kept alive during the v0.1.4 → v0.1.5 transition
    // so existing bookmarks resolve.  Redirects to the new home.
    path: '/admin/feature-flags',
    redirect: '/settings/feature-flags',
  },
  {
    path: '/admin/audit-log',
    name: 'AdminAuditLog',
    component: () => import('../pages/admin/AuditLog.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/admin/backups',
    name: 'AdminBackups',
    component: () => import('../pages/admin/Backups.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    // Legacy alias — redirects to /settings/smtp (v0.1.4).
    path: '/admin/email/smtp',
    redirect: '/settings/smtp',
  },
  {
    path: '/admin/email/templates',
    name: 'AdminEmailTemplates',
    component: () => import('../pages/admin/email/Templates.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },

  // Catch-all: bounce to the dashboard. Router guard then handles redirect to
  // /demo or /login if the user isn't authenticated.
  { path: '/:pathMatch(.*)*', redirect: '/' },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

// ------------------------------------------------------------------
// Global navigation guard.
// ------------------------------------------------------------------

/**
 * Race a promise against a deadline. Rejects with a timeout Error if the
 * deadline fires first, letting callers treat it identically to a 401.
 */
function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) =>
      setTimeout(() => reject(new Error(`guard-timeout:${ms}ms`)), ms),
    ),
  ]);
}

/**
 * Where should an unauthenticated visitor land?
 *
 *  - If the public-demo route (`public-projects`) is registered — the A-方案
 *    agent has landed — send the visitor to /demo for a product preview.
 *  - Otherwise fall back to /login and forward `intendedPath` as ?redirect=
 *    so that post-login deep-links work.
 *
 * Unauth users sent to /demo never get a ?redirect= nudge; they will explore
 * the product and sign in voluntarily from the header button.
 */
function unauthRedirect(intendedPath: string) {
  if (router.hasRoute('public-projects')) {
    return { path: '/demo' };
  }
  const redirect = intendedPath !== '/' ? intendedPath : undefined;
  return {
    path: '/login',
    query: redirect ? { redirect } : undefined,
  };
}

router.beforeEach(async (to: RouteLocationNormalized) => {
  const auth = useAuthStore();
  const needsAuth = to.matched.some((r) => r.meta?.requiresAuth);
  const needsAdmin = to.matched.some((r) => r.meta?.requiresAdmin);
  const isPublic = to.matched.some((r) => r.meta?.public);

  // ------------------------------------------------------------------
  // Token validation (prevents flash-of-authenticated-content).
  //
  // When a token exists in localStorage but the user object hasn't been
  // confirmed for this session yet (cold load after browser restart, tab
  // restored from disk), we synchronously validate the token with the
  // server before making any routing decision.
  //
  // Without this, the old guard would pass `isAuthenticated=true` (token
  // present, not expired per localStorage), let Dashboard mount, and only
  // then receive a 401 from /auth/me — causing the visible flash.
  //
  // A 3-second timeout prevents indefinite hangs on a dead network; on
  // timeout the session is treated as invalid.
  // ------------------------------------------------------------------
  if (auth.accessToken && !auth.currentUser) {
    try {
      await withTimeout(auth.validateSession(), 3_000);
    } catch {
      // 401, network error, or 3-second timeout — session is gone.
      console.log(
        '[router] session validation failed, clearing token and continuing as guest',
      );
      auth.clearSession();
    }
  }

  // Re-read reactive state after the async confirmation above.
  const isAuthenticated = auth.isAuthenticated;

  // 1. Logged-in users bounced away from auth pages.
  if (isAuthenticated && isPublic && ['Login', 'Register'].includes(String(to.name))) {
    return { path: '/' };
  }

  // 2. Protected route without a valid session → /demo or /login.
  if (needsAuth && !isAuthenticated) {
    return unauthRedirect(to.fullPath);
  }

  // 3. Admin-only route, user isn't admin.
  if (needsAdmin && !auth.isAdmin) {
    notification.warning({
      message: 'Admin only',
      description: 'You do not have permission to access that page.',
      duration: 3,
    });
    return { path: '/' };
  }

  return true;
});

export default router;
