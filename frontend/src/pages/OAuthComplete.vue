<script setup lang="ts">
/**
 * /login/oauth/complete landing page.
 *
 * The backend redirects here with a URL fragment like:
 *   #token=<jwt>&email=<e>&login=<github-username>&redirect=/some/path
 *
 * We parse the fragment (never the query string — the JWT must not appear
 * in query strings to keep it out of proxy access logs), hand it to the
 * auth store, then route the user to `redirect` (or `/` by default).
 *
 * If anything is wrong (missing token, bad JSON, server 401 on /me) we
 * bounce the user back to /login with an error query so the toast shows.
 */
import { onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import AuthLayout from '../components/AuthLayout.vue';
import { useAuthStore } from '../store/auth';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const { t } = useI18n();

const errorMsg = ref<string | null>(null);

function parseHash(): Record<string, string> {
  const hash = window.location.hash.startsWith('#')
    ? window.location.hash.slice(1)
    : window.location.hash;
  const result: Record<string, string> = {};
  if (!hash) return result;
  for (const pair of hash.split('&')) {
    if (!pair) continue;
    const [k, v] = pair.split('=');
    if (!k) continue;
    result[decodeURIComponent(k)] = v ? decodeURIComponent(v) : '';
  }
  return result;
}

function goToError(): void {
  // Scrub the fragment so the JWT (if any) doesn't hang around in the URL
  // bar while the router navigates.
  window.location.hash = '';
  void router.replace({
    path: '/login',
    query: { error: 'oauth_github_failed', reason: 'client_parse' },
  });
}

onMounted(async () => {
  // ---- Bind-flow landing: ?bind_ok=1 or ?bind_error=<reason> ------------
  const q = route.query;
  const bindOk = q.bind_ok === '1' || q.bind_ok === 'true';
  const bindError = typeof q.bind_error === 'string' ? q.bind_error : null;
  if (bindOk || bindError) {
    await auth.fetchMe(); // refresh so Profile sees the freshly-linked github_login
    if (bindOk) {
      notification.success({
        message: t('page_settings_profile.github_link_success'),
        duration: 3,
      });
    } else {
      notification.error({
        message: t('page_settings_profile.github_link_failed'),
        description: bindError ?? undefined,
        duration: 4,
      });
    }
    const dest =
      typeof q.redirect === 'string' &&
      q.redirect.startsWith('/') &&
      !q.redirect.startsWith('//')
        ? q.redirect
        : '/settings/profile';
    void router.replace(dest);
    return;
  }

  // ---- Login-flow landing (original behaviour) -------------------------
  const params = parseHash();
  const token = params.token;
  if (!token) {
    errorMsg.value = t('page_oauth.error_generic');
    goToError();
    return;
  }

  // We have no `expires_in` from the hash (only the token) — call /me
  // to both confirm validity and populate the user. We use a synthetic
  // 24h expiry as a floor so the auto-refresh scheduler still picks a
  // sensible window; the server's real TTL takes over after the first
  // /auth/refresh call.
  const FALLBACK_TTL_SECONDS = 24 * 60 * 60;
  auth.setToken(token, FALLBACK_TTL_SECONDS);

  const me = await auth.fetchMe();
  if (!me) {
    auth.clearSession();
    errorMsg.value = t('page_oauth.error_generic');
    goToError();
    return;
  }

  // Scrub the hash before navigating so it doesn't end up in history.
  window.location.hash = '';

  const dest =
    params.redirect && params.redirect.startsWith('/') && !params.redirect.startsWith('//')
      ? params.redirect
      : '/';
  void router.replace(dest);
});
</script>

<template>
  <AuthLayout>
    <a-card :bordered="false" class="auth-card">
      <h2 style="margin-top: 0">{{ t('page_oauth.title') }}</h2>
      <p v-if="!errorMsg" class="muted" style="font-size: 13px">
        {{ t('page_oauth.logging_in') }}
      </p>
      <a-alert
        v-if="errorMsg"
        :message="errorMsg"
        type="error"
        show-icon
        style="margin-top: 16px"
      />
      <div v-if="errorMsg" style="text-align: center; margin-top: 16px; font-size: 13px">
        <router-link to="/login">{{ t('page_oauth.back_to_sign_in') }}</router-link>
      </div>
    </a-card>
  </AuthLayout>
</template>

<style scoped>
.auth-card {
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.35);
}
</style>
