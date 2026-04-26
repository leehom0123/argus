<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { AxiosError } from 'axios';
import AuthLayout from '../components/AuthLayout.vue';
import { requestPasswordReset, resetPassword } from '../api/auth';

const route = useRoute();
const router = useRouter();

// Two UI modes. Decided by presence of ?token=...
// - 'request'  : user enters their email, backend emails a link
// - 'reset'    : user arrived via the emailed link, enters new password
const token = computed<string | null>(() => {
  const t = route.query.token;
  return typeof t === 'string' && t.length > 0 ? t : null;
});
const mode = computed<'request' | 'reset'>(() => (token.value ? 'reset' : 'request'));

// ---- Request mode state ----
const requestForm = reactive({ email: '' });
const requestSent = ref(false);
const requestSubmitting = ref(false);
const requestError = ref<string | null>(null);

async function onRequest() {
  requestError.value = null;
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(requestForm.email.trim())) {
    requestError.value = 'Please enter a valid email address.';
    return;
  }
  requestSubmitting.value = true;
  try {
    await requestPasswordReset({ email: requestForm.email.trim() });
    requestSent.value = true;
  } catch (err) {
    // Backend is designed to always return success to prevent account enumeration,
    // so we land here only on network / 5xx. Show the same UX regardless to
    // avoid leaking info on the edge cases.
    requestSent.value = true;
    if (err instanceof AxiosError && err.response && err.response.status >= 500) {
      requestError.value = 'Server error. Please try again later.';
      requestSent.value = false;
    }
  } finally {
    requestSubmitting.value = false;
  }
}

// ---- Reset mode state ----
const resetForm = reactive({ password: '', confirm: '' });
const resetFieldErrors = reactive({ password: '', confirm: '' });
const resetSubmitting = ref(false);
const resetDone = ref(false);
const resetError = ref<string | null>(null);

function validateResetForm(): boolean {
  resetFieldErrors.password = '';
  resetFieldErrors.confirm = '';
  if (resetForm.password.length < 10) {
    resetFieldErrors.password = 'Password must be at least 10 characters.';
  } else if (!/[A-Za-z]/.test(resetForm.password) || !/\d/.test(resetForm.password)) {
    resetFieldErrors.password = 'Password must contain at least one letter and one digit.';
  }
  if (resetForm.password !== resetForm.confirm) {
    resetFieldErrors.confirm = 'Passwords do not match.';
  }
  return !resetFieldErrors.password && !resetFieldErrors.confirm;
}

async function onReset() {
  resetError.value = null;
  if (!validateResetForm()) return;
  if (!token.value) {
    resetError.value = 'No reset token present.';
    return;
  }
  resetSubmitting.value = true;
  try {
    await resetPassword({ token: token.value, new_password: resetForm.password });
    resetDone.value = true;
    window.setTimeout(() => {
      void router.push('/login');
    }, 2000);
  } catch (err) {
    if (err instanceof AxiosError) {
      const status = err.response?.status;
      const detail = (err.response?.data as { detail?: string } | undefined)?.detail;
      if (status === 422) {
        resetFieldErrors.password = detail ?? 'Password does not meet requirements.';
      } else if (status === 400 || status === 404) {
        resetError.value = 'This reset link is invalid or has expired. Request a new one.';
      } else {
        resetError.value = detail ?? 'Password reset failed.';
      }
    } else {
      resetError.value = 'Password reset failed.';
    }
  } finally {
    resetSubmitting.value = false;
  }
}
</script>

<template>
  <AuthLayout>
    <a-card :bordered="false" class="auth-card">
      <!-- ---------- Mode: request reset email ---------- -->
      <template v-if="mode === 'request'">
        <template v-if="!requestSent">
          <h2 style="margin-top: 0; margin-bottom: 4px">Reset password</h2>
          <p class="muted" style="margin-top: 0; margin-bottom: 20px; font-size: 13px">
            Enter your email and we'll send you a reset link.
          </p>

          <a-alert
            v-if="requestError"
            :message="requestError"
            type="error"
            show-icon
            style="margin-bottom: 16px"
          />

          <a-form layout="vertical" @submit.prevent="onRequest">
            <a-form-item label="Email" required>
              <a-input
                v-model:value="requestForm.email"
                placeholder="you@example.com"
                autocomplete="email"
                :disabled="requestSubmitting"
              />
            </a-form-item>
            <a-button type="primary" html-type="submit" block :loading="requestSubmitting">
              Send reset link
            </a-button>
          </a-form>

          <a-divider style="margin: 20px 0 12px" />
          <div style="text-align: center; font-size: 13px">
            <router-link to="/login">Back to sign in</router-link>
          </div>
        </template>

        <template v-else>
          <a-result
            status="info"
            title="Check your email"
            sub-title="If an account with that email exists, we've sent a password reset link. It expires in 15 minutes."
          >
            <template #extra>
              <router-link to="/login">
                <a-button type="primary">Back to sign in</a-button>
              </router-link>
            </template>
          </a-result>
        </template>
      </template>

      <!-- ---------- Mode: set new password ---------- -->
      <template v-else>
        <template v-if="!resetDone">
          <h2 style="margin-top: 0; margin-bottom: 4px">Choose a new password</h2>
          <p class="muted" style="margin-top: 0; margin-bottom: 20px; font-size: 13px">
            The reset link opens this page. Enter a new password to finish.
          </p>

          <a-alert
            v-if="resetError"
            :message="resetError"
            type="error"
            show-icon
            style="margin-bottom: 16px"
          />

          <a-form layout="vertical" @submit.prevent="onReset">
            <a-form-item
              label="New password"
              required
              :validate-status="resetFieldErrors.password ? 'error' : undefined"
              :help="
                resetFieldErrors.password ||
                'At least 10 characters, with at least one letter and one digit.'
              "
            >
              <a-input-password
                v-model:value="resetForm.password"
                autocomplete="new-password"
                :disabled="resetSubmitting"
              />
            </a-form-item>

            <a-form-item
              label="Confirm new password"
              required
              :validate-status="resetFieldErrors.confirm ? 'error' : undefined"
              :help="resetFieldErrors.confirm || undefined"
            >
              <a-input-password
                v-model:value="resetForm.confirm"
                autocomplete="new-password"
                :disabled="resetSubmitting"
              />
            </a-form-item>

            <a-button type="primary" html-type="submit" block :loading="resetSubmitting">
              Reset password
            </a-button>
          </a-form>
        </template>

        <template v-else>
          <a-result
            status="success"
            title="Password updated"
            sub-title="Redirecting you to sign in…"
          />
        </template>
      </template>
    </a-card>
  </AuthLayout>
</template>

<style scoped>
.auth-card {
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.35);
}
</style>
