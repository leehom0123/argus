import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import router from './router';
import i18n from './i18n';
import { useAuthStore } from './store/auth';
import { useAppStore } from './store/app';
import { useHintsStore } from './store/hints';
import { registerUnauthorizedHandler } from './api/client';
import { migrateLegacyStorage } from './utils/storage_migrator';
import './styles.css';

// Scrub abandoned pre-v0.1.0 `em_*` localStorage keys exactly once. Must run
// BEFORE Pinia / router / i18n initialisation, since the stores read
// localStorage synchronously at construction time and we don't want them
// observing legacy values mid-clean.
migrateLegacyStorage();

const app = createApp(App);
const pinia = createPinia();
app.use(pinia);
app.use(router);
app.use(i18n);

// Apply dark-mode class immediately — before mount — so CSS variables and
// global styles switch without a frame of wrong colours on first paint.
useAppStore().applyStoredDark();

// Wire axios interceptor → auth store BEFORE mount so any bootstrap request
// that fires a 401 is routed through the same handler.
const auth = useAuthStore();
registerUnauthorizedHandler(() => {
  auth.clearSession();
  // Preserve the user's intended destination so they bounce back after login.
  const current = router.currentRoute.value;
  const redirect = current.fullPath && current.fullPath !== '/login' ? current.fullPath : undefined;
  void router.push({
    path: '/login',
    query: redirect && !redirect.startsWith('/login') ? { redirect } : undefined,
  });
});

// Kick off a background session check. We don't await — the router guard will
// gate protected pages on auth.isAuthenticated, which reads localStorage
// synchronously. /auth/me runs in parallel to upgrade stale cached user info.
void auth.bootstrap();

// Preload the empty-state hint catalog (#30) so EmptyState components
// render a localised hint on first paint rather than flickering through
// the i18n fallback. The endpoint is small (~11 short strings) and public.
void useHintsStore().ensureLoaded();

app.mount('#app');
