<script setup lang="ts">
/**
 * Settings > Change password (Team Pwd).
 *
 * Three inputs (current / new / confirm) with inline validation:
 *   - new password: min 10 chars, letters + digits (mirrors backend)
 *   - confirm matches new
 *   - new must differ from current
 *
 * On success:
 *   - toast notification
 *   - redirect to / (Dashboard). The current JWT stays valid so we don't
 *     have to log out; sibling sessions are revoked server-side.
 *
 * On 401 (wrong current): show inline error above the current-password
 * input instead of the global toast from the http interceptor.
 */
import { computed, reactive, ref } from 'vue';
import { AxiosError } from 'axios';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { useRouter } from 'vue-router';
import { changePassword } from '../../api/auth';

const { t } = useI18n();
const router = useRouter();

const form = reactive({
  current: '',
  next: '',
  confirm: '',
});

const submitting = ref(false);
/** Inline error shown under the current-password field after a 401. */
const wrongCurrentError = ref<string | null>(null);
/** Inline error shown under the submit button after a 429. */
const rateLimitError = ref<string | null>(null);

// ---- client-side validation -------------------------------------------
const newPasswordMinOk = computed(() => form.next.length >= 10);
const newPasswordHasMix = computed(
  () => /[A-Za-z]/.test(form.next) && /\d/.test(form.next),
);
const newPasswordValid = computed(
  () => newPasswordMinOk.value && newPasswordHasMix.value,
);
const confirmMatches = computed(
  () => form.confirm.length > 0 && form.confirm === form.next,
);
const differsFromCurrent = computed(
  () => form.next.length > 0 && form.next !== form.current,
);

const canSubmit = computed(
  () =>
    !submitting.value &&
    form.current.length > 0 &&
    newPasswordValid.value &&
    confirmMatches.value &&
    differsFromCurrent.value,
);

async function onSubmit(): Promise<void> {
  if (!canSubmit.value) return;
  wrongCurrentError.value = null;
  rateLimitError.value = null;
  submitting.value = true;
  try {
    await changePassword(form.current, form.next);
    notification.success({
      message: t('page_settings_password.toast_success'),
      description: t('page_settings_password.toast_success_desc'),
      duration: 4,
    });
    await router.push('/');
  } catch (err) {
    if (err instanceof AxiosError) {
      if (err.response?.status === 401) {
        wrongCurrentError.value = t('page_settings_password.error_wrong_current');
        return;
      }
      if (err.response?.status === 429) {
        rateLimitError.value = t('page_settings_password.error_rate_limited');
        return;
      }
    }
    // Other errors (400 same-as-current, 422 too short) are already
    // surfaced by the global http interceptor; we don't duplicate here.
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="page-container" style="max-width: 720px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>
        {{ $t('page_settings_password.breadcrumb_settings') }}
      </a-breadcrumb-item>
      <a-breadcrumb-item>
        {{ $t('page_settings_password.breadcrumb_password') }}
      </a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="$t('page_settings_password.card_title')">
      <a-alert
        type="info"
        show-icon
        :message="$t('page_settings_password.alert_desc')"
        style="margin-bottom: 16px"
      />

      <a-form layout="vertical" @submit.prevent="onSubmit">
        <a-form-item
          :label="$t('page_settings_password.label_current')"
          :validate-status="wrongCurrentError ? 'error' : ''"
          :help="wrongCurrentError || ''"
        >
          <a-input-password
            v-model:value="form.current"
            autocomplete="current-password"
          />
        </a-form-item>

        <a-form-item
          :label="$t('page_settings_password.label_new')"
          :validate-status="
            form.next.length > 0 && !newPasswordValid ? 'error' : ''
          "
          :help="
            form.next.length > 0 && !newPasswordValid
              ? $t('page_settings_password.hint_min_length')
              : $t('page_settings_password.hint_min_length')
          "
        >
          <a-input-password
            v-model:value="form.next"
            autocomplete="new-password"
          />
        </a-form-item>

        <a-form-item
          :label="$t('page_settings_password.label_confirm')"
          :validate-status="
            form.confirm.length > 0 && !confirmMatches ? 'error' : ''
          "
          :help="
            form.confirm.length > 0 && !confirmMatches
              ? $t('page_settings_password.hint_no_match')
              : ''
          "
        >
          <a-input-password
            v-model:value="form.confirm"
            autocomplete="new-password"
          />
        </a-form-item>

        <a-form-item
          v-if="
            form.next.length > 0 &&
            form.current.length > 0 &&
            !differsFromCurrent
          "
          :validate-status="'error'"
          :help="$t('page_settings_password.hint_same_as_current')"
        />

        <a-form-item
          v-if="rateLimitError"
          :validate-status="'error'"
          :help="rateLimitError"
        />

        <a-form-item>
          <a-button
            type="primary"
            html-type="submit"
            :disabled="!canSubmit"
            :loading="submitting"
          >
            {{ $t('page_settings_password.btn_submit') }}
          </a-button>
        </a-form-item>
      </a-form>
    </a-card>
  </div>
</template>
