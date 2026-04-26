<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { AxiosError } from 'axios';
import AuthLayout from '../components/AuthLayout.vue';
import { verifyEmail } from '../api/auth';

const route = useRoute();

type State = 'verifying' | 'success' | 'error';
const state = ref<State>('verifying');
const errorMsg = ref<string>('');

onMounted(async () => {
  const token = typeof route.query.token === 'string' ? route.query.token : '';
  if (!token) {
    state.value = 'error';
    errorMsg.value = 'No verification token found in the link.';
    return;
  }
  try {
    await verifyEmail({ token });
    state.value = 'success';
  } catch (err) {
    state.value = 'error';
    if (err instanceof AxiosError) {
      const detail = (err.response?.data as { detail?: string } | undefined)?.detail;
      errorMsg.value =
        detail ?? 'The verification link is invalid, expired, or has already been used.';
    } else {
      errorMsg.value = 'Verification failed. Please try again later.';
    }
  }
});
</script>

<template>
  <AuthLayout>
    <a-card :bordered="false" class="auth-card">
      <template v-if="state === 'verifying'">
        <div style="text-align: center; padding: 24px 0">
          <a-spin size="large" />
          <p style="margin-top: 16px">Verifying your email…</p>
        </div>
      </template>

      <template v-else-if="state === 'success'">
        <a-result
          status="success"
          title="Email verified"
          sub-title="Your email address has been confirmed. You can sign in now."
        >
          <template #extra>
            <router-link to="/login">
              <a-button type="primary">Go to sign in</a-button>
            </router-link>
          </template>
        </a-result>
      </template>

      <template v-else>
        <a-result
          status="error"
          title="Verification failed"
          :sub-title="errorMsg"
        >
          <template #extra>
            <div style="display: flex; gap: 8px; justify-content: center; flex-wrap: wrap">
              <router-link to="/login">
                <a-button>Back to sign in</a-button>
              </router-link>
              <router-link to="/register">
                <a-button type="primary">Register again</a-button>
              </router-link>
            </div>
            <p class="muted" style="margin-top: 16px; font-size: 12px">
              If you believe this is a mistake, contact an admin to resend your verification email.
            </p>
          </template>
        </a-result>
      </template>
    </a-card>
  </AuthLayout>
</template>

<style scoped>
.auth-card {
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.35);
}
</style>
