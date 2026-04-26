<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
import { AxiosError } from 'axios';
import AuthLayout from '../components/AuthLayout.vue';
import { useAuthStore } from '../store/auth';

const auth = useAuthStore();

const form = reactive({
  username: '',
  email: '',
  password: '',
  confirm: '',
});

// Field-level errors (mapped from server 409 / 422 responses)
const fieldErrors = reactive<Record<string, string>>({
  username: '',
  email: '',
  password: '',
  confirm: '',
});

const submitting = ref(false);
const submitted = ref(false);
const topError = ref<string | null>(null);

// ---- Client-side validation ----

function validateAll(): boolean {
  fieldErrors.username = '';
  fieldErrors.email = '';
  fieldErrors.password = '';
  fieldErrors.confirm = '';

  if (form.username.trim().length < 3) {
    fieldErrors.username = 'Username must be at least 3 characters.';
  }
  // basic RFC-ish email check — backend is the real authority
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.trim())) {
    fieldErrors.email = 'Please enter a valid email address.';
  }
  if (form.password.length < 10) {
    fieldErrors.password = 'Password must be at least 10 characters.';
  } else if (!/[A-Za-z]/.test(form.password) || !/\d/.test(form.password)) {
    fieldErrors.password = 'Password must contain at least one letter and one digit.';
  }
  if (form.password !== form.confirm) {
    fieldErrors.confirm = 'Passwords do not match.';
  }

  return !Object.values(fieldErrors).some(Boolean);
}

// ---- Password strength meter ----
// Simple heuristic — not a security boundary, just UX feedback.
const strength = computed(() => {
  const pw = form.password;
  if (!pw) return { percent: 0, status: 'exception' as const, label: '' };
  let score = 0;
  if (pw.length >= 10) score += 25;
  if (pw.length >= 14) score += 15;
  if (/[A-Z]/.test(pw)) score += 15;
  if (/[a-z]/.test(pw)) score += 10;
  if (/\d/.test(pw)) score += 15;
  if (/[^A-Za-z0-9]/.test(pw)) score += 20;
  score = Math.min(100, score);
  let label = 'weak';
  let status: 'exception' | 'normal' | 'active' | 'success' = 'exception';
  if (score >= 80) {
    label = 'strong';
    status = 'success';
  } else if (score >= 55) {
    label = 'fair';
    status = 'active';
  } else if (score >= 30) {
    label = 'poor';
    status = 'normal';
  }
  return { percent: score, status, label };
});

async function onSubmit() {
  topError.value = null;
  if (!validateAll()) return;

  submitting.value = true;
  try {
    await auth.register({
      username: form.username.trim(),
      email: form.email.trim(),
      password: form.password,
    });
    submitted.value = true;
  } catch (err) {
    if (err instanceof AxiosError) {
      const status = err.response?.status;
      const detail = (err.response?.data as { detail?: string } | undefined)?.detail;

      if (status === 409) {
        // Username or email already exists. Backend may not specify which;
        // show a generic message if we can't tell from the detail text.
        const msg = detail ?? 'Username or email already in use.';
        if (/email/i.test(msg)) fieldErrors.email = msg;
        else if (/user/i.test(msg)) fieldErrors.username = msg;
        else topError.value = msg;
      } else if (status === 422) {
        const msg = detail ?? 'Password does not meet requirements.';
        fieldErrors.password = msg;
      } else {
        topError.value = detail ?? 'Registration failed. Please try again.';
      }
    } else {
      topError.value = 'Registration failed. Please try again.';
    }
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <AuthLayout>
    <a-card :bordered="false" class="auth-card">
      <template v-if="!submitted">
        <h2 style="margin-top: 0; margin-bottom: 4px">Create account</h2>
        <p class="muted" style="margin-top: 0; margin-bottom: 20px; font-size: 13px">
          Register to track experiments across hosts.
        </p>

        <a-alert
          v-if="topError"
          :message="topError"
          type="error"
          show-icon
          style="margin-bottom: 16px"
        />

        <a-form layout="vertical" @submit.prevent="onSubmit">
          <a-form-item
            label="Username"
            required
            :validate-status="fieldErrors.username ? 'error' : undefined"
            :help="fieldErrors.username || undefined"
          >
            <a-input
              v-model:value="form.username"
              placeholder="3+ characters"
              autocomplete="username"
              :disabled="submitting"
            />
          </a-form-item>

          <a-form-item
            label="Email"
            required
            :validate-status="fieldErrors.email ? 'error' : undefined"
            :help="fieldErrors.email || undefined"
          >
            <a-input
              v-model:value="form.email"
              placeholder="you@example.com"
              autocomplete="email"
              :disabled="submitting"
            />
          </a-form-item>

          <a-form-item
            label="Password"
            required
            :validate-status="fieldErrors.password ? 'error' : undefined"
            :help="
              fieldErrors.password ||
              'At least 10 characters, with at least one letter and one digit.'
            "
          >
            <a-input-password
              v-model:value="form.password"
              placeholder="••••••••"
              autocomplete="new-password"
              :disabled="submitting"
            />
            <div v-if="form.password" style="margin-top: 8px">
              <a-progress
                :percent="strength.percent"
                :status="strength.status"
                size="small"
                :show-info="false"
              />
              <div class="muted" style="font-size: 11px; margin-top: 2px">
                Strength: {{ strength.label }}
              </div>
            </div>
          </a-form-item>

          <a-form-item
            label="Confirm password"
            required
            :validate-status="fieldErrors.confirm ? 'error' : undefined"
            :help="fieldErrors.confirm || undefined"
          >
            <a-input-password
              v-model:value="form.confirm"
              placeholder="••••••••"
              autocomplete="new-password"
              :disabled="submitting"
            />
          </a-form-item>

          <a-button type="primary" html-type="submit" block :loading="submitting">
            Create account
          </a-button>
        </a-form>

        <a-divider style="margin: 20px 0 12px" />
        <div style="text-align: center; font-size: 13px">
          Already have an account?
          <router-link to="/login">Sign in</router-link>
        </div>
      </template>

      <template v-else>
        <a-result
          status="success"
          title="Check your email"
          sub-title="We've sent you a verification link. Click it to activate your account — you can log in either before or after verifying."
        >
          <template #extra>
            <router-link to="/login">
              <a-button type="primary">Back to sign in</a-button>
            </router-link>
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
