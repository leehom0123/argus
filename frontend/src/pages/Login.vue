<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { AxiosError } from 'axios';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import AuthLayout from '../components/AuthLayout.vue';
import { useAuthStore } from '../store/auth';
import { LoginLockedError, getOAuthConfig } from '../api/client';
import type { LoginIn } from '../types';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const { t } = useI18n();

const form = reactive<LoginIn & { remember: boolean }>({
  username_or_email: '',
  password: '',
  remember: true,
});

const submitting = ref(false);
const errorMsg = ref<string | null>(null);

// OAuth feature-detect. Hidden until the server confirms github=true.
const oauthGithubEnabled = ref(false);

onMounted(async () => {
  try {
    const cfg = await getOAuthConfig();
    oauthGithubEnabled.value = cfg.github;
  } catch {
    oauthGithubEnabled.value = false;
  }
});

// If we bounced back from the OAuth callback with an error, toast once.
function maybeShowOAuthError(): void {
  const err = route.query.error;
  if (err === 'oauth_github_failed') {
    notification.error({
      message: t('page_oauth.error_generic'),
      duration: 4,
    });
    // Strip the query so a refresh doesn't re-toast.
    void router.replace({ path: '/login', query: {} });
  }
}
onMounted(maybeShowOAuthError);
watch(() => route.query.error, maybeShowOAuthError);

// 423 Locked countdown
const lockSecondsLeft = ref(0);
let lockTimer: number | null = null;

const lockDisplay = computed(() => {
  const s = lockSecondsLeft.value;
  if (s <= 0) return '';
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
});

function startLockCountdown(seconds: number) {
  stopLockCountdown();
  lockSecondsLeft.value = seconds;
  lockTimer = window.setInterval(() => {
    lockSecondsLeft.value -= 1;
    if (lockSecondsLeft.value <= 0) {
      stopLockCountdown();
    }
  }, 1000);
}
function stopLockCountdown() {
  if (lockTimer !== null) {
    window.clearInterval(lockTimer);
    lockTimer = null;
  }
}
onUnmounted(stopLockCountdown);

async function onSubmit() {
  errorMsg.value = null;
  submitting.value = true;
  try {
    await auth.login({
      username_or_email: form.username_or_email.trim(),
      password: form.password,
    });
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/';
    await router.push(redirect || '/');
  } catch (err) {
    if (err instanceof LoginLockedError) {
      startLockCountdown(err.retry_after);
      errorMsg.value = t('page_login.error_locked', { s: err.retry_after });
    } else if (err instanceof AxiosError) {
      const status = err.response?.status;
      const detail =
        (err.response?.data as { detail?: string } | undefined)?.detail ??
        t('page_login.error_generic');
      if (status === 401) {
        errorMsg.value = t('page_login.error_invalid');
      } else if (status === 422) {
        errorMsg.value = detail;
      } else {
        errorMsg.value = detail;
      }
    } else {
      errorMsg.value = t('page_login.error_generic');
    }
  } finally {
    submitting.value = false;
  }
}

function signInWithGithub(): void {
  // Server starts the OAuth dance. We forward any ``?redirect=`` param
  // so the post-login destination survives the round trip.
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '';
  const qs = redirect ? `?redirect=${encodeURIComponent(redirect)}` : '';
  // Hard navigation (not router.push) — we're leaving the SPA for GitHub.
  window.location.href = `/api/auth/oauth/github/start${qs}`;
}
</script>

<template>
  <AuthLayout>
    <a-card :bordered="false" class="auth-card">
      <h2 style="margin-top: 0; margin-bottom: 4px">{{ t('page_login.title') }}</h2>
      <p class="muted" style="margin-top: 0; margin-bottom: 20px; font-size: 13px">
        {{ t('page_login.subtitle') }}
      </p>

      <a-alert
        v-if="errorMsg"
        :message="errorMsg"
        type="error"
        show-icon
        style="margin-bottom: 16px"
      />
      <a-alert
        v-if="lockSecondsLeft > 0"
        type="warning"
        show-icon
        :message="t('page_login.locked', { time: lockDisplay })"
        style="margin-bottom: 16px"
      />

      <a-form :model="form" layout="vertical" @submit.prevent="onSubmit">
        <a-form-item
          :label="t('page_login.label_username_or_email')"
          name="username_or_email"
        >
          <a-input
            v-model:value="form.username_or_email"
            :placeholder="t('page_login.placeholder_username_or_email')"
            autocomplete="username"
            :disabled="submitting"
          />
        </a-form-item>

        <a-form-item :label="t('page_login.label_password')" name="password">
          <a-input-password
            v-model:value="form.password"
            placeholder="••••••••"
            autocomplete="current-password"
            :disabled="submitting"
          />
        </a-form-item>

        <div
          style="
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
          "
        >
          <a-checkbox v-model:checked="form.remember">{{ t('auth.remember_me') }}</a-checkbox>
          <router-link to="/reset-password" style="font-size: 12px">
            {{ t('auth.forgot_password') }}
          </router-link>
        </div>

        <a-button
          type="primary"
          html-type="submit"
          block
          :loading="submitting"
          :disabled="lockSecondsLeft > 0"
        >
          {{ t('page_login.submit') }}
        </a-button>
      </a-form>

      <template v-if="oauthGithubEnabled">
        <a-divider style="margin: 20px 0 12px">
          <span class="muted" style="font-size: 12px">{{ t('page_oauth.or') }}</span>
        </a-divider>
        <a-button block :disabled="submitting" @click="signInWithGithub">
          <template #icon>
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              width="16"
              height="16"
              style="vertical-align: middle; margin-right: 6px"
              aria-hidden="true"
            >
              <path
                fill="currentColor"
                d="M12 .5C5.65.5.5 5.65.5 12a11.5 11.5 0 0 0 7.86 10.94c.57.1.78-.25.78-.55v-1.9c-3.2.7-3.88-1.54-3.88-1.54-.52-1.33-1.27-1.68-1.27-1.68-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.18 1.76 1.18 1.02 1.76 2.69 1.25 3.35.95.1-.74.4-1.25.72-1.54-2.55-.29-5.24-1.28-5.24-5.68 0-1.26.45-2.29 1.18-3.1-.12-.29-.51-1.46.11-3.04 0 0 .96-.31 3.16 1.18a10.96 10.96 0 0 1 5.75 0c2.2-1.49 3.16-1.18 3.16-1.18.62 1.58.23 2.75.11 3.04.74.81 1.18 1.84 1.18 3.1 0 4.41-2.69 5.38-5.25 5.67.41.36.78 1.05.78 2.13v3.16c0 .3.21.66.79.55A11.5 11.5 0 0 0 23.5 12C23.5 5.65 18.35.5 12 .5z"
              />
            </svg>
          </template>
          {{ t('page_oauth.sign_in_with_github') }}
        </a-button>
      </template>

      <a-divider style="margin: 20px 0 12px" />
      <div style="text-align: center; font-size: 13px">
        {{ t('page_login.no_account') }}
        <router-link to="/register">{{ t('page_login.create_one') }}</router-link>
      </div>
    </a-card>
  </AuthLayout>
</template>

<style scoped>
.auth-card {
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.35);
}
</style>
