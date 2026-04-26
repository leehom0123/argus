<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { AxiosError } from 'axios';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { useAuthStore } from '../../store/auth';
import { fmtTime } from '../../utils/format';
import {
  changeEmail,
  githubLinkStart,
  githubSetPassword,
  githubUnlink,
} from '../../api/auth';
import { resendVerification } from '../../api/me';

const auth = useAuthStore();
const { t } = useI18n();
const loading = ref(false);

// GitHub section state. Truth comes from /auth/me (has_password,
// github_login). Unlink has two shapes: user has a password → simple
// popconfirm; no password yet → set-password modal first.
const bindError = ref<string | null>(null);
const setPasswordModalOpen = ref(false);
const setPasswordModalIntent = ref<'unlink' | 'proactive'>('proactive');
const setPasswordForm = reactive<{ new_password: string; confirm: string }>({
  new_password: '',
  confirm: '',
});
const setPasswordSubmitting = ref(false);
const setPasswordError = ref<string | null>(null);
const unlinkSubmitting = ref(false);

// Email-change modal state. ``current_password`` is re-collected here
// even though the user is already authenticated — the backend uses it
// as a fresh proof-of-identity check before mailing the confirm link
// (mirrors how change-password re-prompts).
const emailModalOpen = ref(false);
const emailForm = reactive<{ new_email: string; current_password: string }>({
  new_email: '',
  current_password: '',
});
const emailSubmitting = ref(false);
const emailError = ref<string | null>(null);

const githubLinked = computed(() => !!auth.currentUser?.github_login);
const hasPassword = computed(() => auth.currentUser?.has_password === true);
const githubOnly = computed(() => githubLinked.value && !hasPassword.value);

// ---- Resend-verification banner state (#108) ----------------------------
// Shown when ``email_verified === false``. Cooldown mirrors the backend's
// 1/min/user rate limit so the button visibly disables after a press
// instead of letting users hammer it and collect 429s.
const resendSubmitting = ref(false);
const resendCooldownUntil = ref<number>(0);
const resendCooldownRemaining = ref<number>(0);
let resendCooldownTimer: ReturnType<typeof setInterval> | null = null;

const showVerifyBanner = computed(() => {
  if (!auth.currentUser) return false;
  return auth.currentUser.email_verified === false;
});

const resendDisabled = computed<boolean>(() => {
  return resendSubmitting.value || resendCooldownRemaining.value > 0;
});

function startResendCooldown(seconds: number): void {
  resendCooldownUntil.value = Date.now() + seconds * 1000;
  resendCooldownRemaining.value = seconds;
  if (resendCooldownTimer) clearInterval(resendCooldownTimer);
  resendCooldownTimer = setInterval(() => {
    const remaining = Math.max(
      0,
      Math.ceil((resendCooldownUntil.value - Date.now()) / 1000),
    );
    resendCooldownRemaining.value = remaining;
    if (remaining <= 0 && resendCooldownTimer) {
      clearInterval(resendCooldownTimer);
      resendCooldownTimer = null;
    }
  }, 1000);
}

async function doResendVerification(): Promise<void> {
  resendSubmitting.value = true;
  try {
    await resendVerification();
    notification.success({
      message: t('page_settings_profile.verify_resent_toast'),
      duration: 4,
    });
    // Backend rate-limit is 1/min/user — local cooldown matches so the
    // button can't be re-enabled before the bucket has a token again.
    startResendCooldown(60);
  } catch (err) {
    if (err instanceof AxiosError) {
      // 409 → email got verified in another tab between page load and
      // click. Refetch ``/auth/me`` so the banner disappears + show a
      // friendly toast rather than a generic error.
      if (err.response?.status === 409) {
        notification.info({
          message: t('page_settings_profile.verify_already_verified_toast'),
          duration: 3,
        });
        await auth.fetchMe();
        return;
      }
      // 429 → rate-limited. Honour Retry-After when present.
      if (err.response?.status === 429) {
        const retryAfter = Number(err.response.headers?.['retry-after']);
        const seconds = Number.isFinite(retryAfter) && retryAfter > 0
          ? Math.ceil(retryAfter)
          : 60;
        startResendCooldown(seconds);
        notification.warning({
          message: t('page_settings_profile.verify_rate_limited_toast', {
            seconds,
          }),
          duration: 4,
        });
        return;
      }
    }
    notification.error({
      message: t('page_settings_profile.verify_resend_failed_toast'),
      duration: 4,
    });
  } finally {
    resendSubmitting.value = false;
  }
}

async function bindGithub(): Promise<void> {
  // POST to /link/init first (axios sends bearer token), then hard-navigate.
  // Previously window.location.href = githubLinkStartUrl(...) was used here,
  // but browser navigation does not send the Authorization header → 401.
  try {
    const { authorize_url } = await githubLinkStart('/settings/profile');
    window.location.href = authorize_url;
  } catch {
    notification.error({ message: t('page_settings_profile.github_link_failed') });
  }
}

async function doUnlink(): Promise<void> {
  unlinkSubmitting.value = true;
  try {
    await githubUnlink();
    notification.success({
      message: t('page_settings_profile.github_unlink_success'),
      duration: 2,
    });
    await auth.fetchMe();
  } catch (err) {
    const detail = extractAxiosDetail(err) ?? t('page_settings_profile.github_unlink_failed');
    if (err instanceof AxiosError && err.response?.status === 409) {
      // Likely "no password set"; open the set-password modal as a
      // recovery path.
      setPasswordModalIntent.value = 'unlink';
      setPasswordModalOpen.value = true;
      setPasswordError.value = detail;
    } else {
      notification.error({ message: detail, duration: 4 });
    }
  } finally {
    unlinkSubmitting.value = false;
  }
}

function openSetPasswordModal(intent: 'unlink' | 'proactive' = 'proactive'): void {
  setPasswordModalIntent.value = intent;
  setPasswordForm.new_password = '';
  setPasswordForm.confirm = '';
  setPasswordError.value = null;
  setPasswordModalOpen.value = true;
}

async function submitSetPassword(): Promise<void> {
  setPasswordError.value = null;
  const pw = setPasswordForm.new_password;
  if (pw.length < 10) {
    setPasswordError.value = t('page_register.validation_password_short');
    return;
  }
  if (!/[A-Za-z]/.test(pw) || !/[0-9]/.test(pw)) {
    setPasswordError.value = t('page_register.validation_password_weak');
    return;
  }
  if (pw !== setPasswordForm.confirm) {
    setPasswordError.value = t('page_register.validation_password_mismatch');
    return;
  }

  setPasswordSubmitting.value = true;
  try {
    await githubSetPassword(pw);
    await auth.fetchMe();
    notification.success({
      message: t('page_settings_profile.github_set_password_success'),
      duration: 2,
    });
    if (setPasswordModalIntent.value === 'unlink') {
      setPasswordModalOpen.value = false;
      await doUnlink();
    } else {
      setPasswordModalOpen.value = false;
    }
  } catch (err) {
    setPasswordError.value =
      extractAxiosDetail(err) ?? t('page_settings_profile.github_set_password_failed');
  } finally {
    setPasswordSubmitting.value = false;
  }
}

function openEmailModal(): void {
  emailForm.new_email = '';
  emailForm.current_password = '';
  emailError.value = null;
  emailModalOpen.value = true;
}

async function submitEmailChange(): Promise<void> {
  emailError.value = null;
  const next = emailForm.new_email.trim().toLowerCase();
  if (!next || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(next)) {
    emailError.value = t('page_settings_profile.email_modal_new_email');
    return;
  }
  if (!emailForm.current_password) {
    emailError.value = t('page_settings_profile.email_change_error_wrong_password');
    return;
  }

  emailSubmitting.value = true;
  try {
    await changeEmail(next, emailForm.current_password);
    notification.success({
      message: t('page_settings_profile.email_change_success', { email: next }),
      duration: 6,
    });
    emailModalOpen.value = false;
  } catch (err) {
    if (err instanceof AxiosError && err.response?.status === 401) {
      emailError.value = t('page_settings_profile.email_change_error_wrong_password');
    } else {
      emailError.value =
        extractAxiosDetail(err)
        ?? t('page_settings_profile.email_change_error_wrong_password');
    }
  } finally {
    emailSubmitting.value = false;
  }
}

function extractAxiosDetail(err: unknown): string | null {
  if (err instanceof AxiosError) {
    const d = (err.response?.data as { detail?: string } | undefined)?.detail;
    if (typeof d === 'string' && d.length > 0) return d;
  }
  return null;
}

onMounted(async () => {
  if (auth.accessToken) {
    loading.value = true;
    try {
      await auth.fetchMe();
    } finally {
      loading.value = false;
    }
  }
});
</script>

<template>
  <div class="page-container" style="max-width: 720px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_settings_profile.breadcrumb_settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_settings_profile.breadcrumb_profile') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <!--
      Email-verify resend banner (#108). Surfaces above the tabs because
      an unverified email blocks several downstream features (e.g. some
      of the email-driven notification flows fall back to the registered
      address) — putting it inline with the other Profile fields would
      bury the call to action.
    -->
    <a-alert
      v-if="showVerifyBanner"
      type="warning"
      show-icon
      data-testid="verify-banner"
      :message="$t('page_settings_profile.verify_banner_message')"
      :description="$t('page_settings_profile.verify_banner_description')"
      style="margin-bottom: 16px"
    >
      <template #action>
        <a-button
          type="primary"
          size="small"
          :loading="resendSubmitting"
          :disabled="resendDisabled"
          data-testid="verify-resend-btn"
          @click="doResendVerification"
        >
          <template v-if="resendCooldownRemaining > 0">
            {{ $t('page_settings_profile.verify_resend_cooldown', {
              seconds: resendCooldownRemaining,
            }) }}
          </template>
          <template v-else>
            {{ $t('page_settings_profile.verify_resend_button') }}
          </template>
        </a-button>
      </template>
    </a-alert>

    <a-tabs :active-key="'profile'">
      <a-tab-pane key="profile" :tab="$t('page_settings_profile.tab_profile')">
        <a-card :title="$t('page_settings_profile.card_account')" :loading="loading" style="margin-top: 8px">
          <a-descriptions :column="1" bordered size="small">
            <a-descriptions-item :label="$t('page_settings_profile.label_user_id')">
              {{ auth.currentUser?.id ?? '—' }}
            </a-descriptions-item>
            <a-descriptions-item :label="$t('page_settings_profile.label_username')">
              {{ auth.currentUser?.username ?? '—' }}
            </a-descriptions-item>
            <a-descriptions-item :label="$t('page_settings_profile.label_email')">
              <span>{{ auth.currentUser?.email ?? '—' }}</span>
              <a-tag v-if="auth.currentUser?.email_verified" color="green" style="margin-left: 8px">
                {{ $t('page_settings_profile.tag_verified') }}
              </a-tag>
              <a-tag v-else color="orange" style="margin-left: 8px">{{ $t('page_settings_profile.tag_unverified') }}</a-tag>
            </a-descriptions-item>
            <a-descriptions-item :label="$t('page_settings_profile.label_role')">
              <a-tag v-if="auth.isAdmin" color="gold">{{ $t('page_settings_profile.tag_admin') }}</a-tag>
              <a-tag v-else>{{ $t('page_settings_profile.tag_user') }}</a-tag>
            </a-descriptions-item>
            <a-descriptions-item :label="$t('page_settings_profile.label_created')">
              {{ fmtTime(auth.currentUser?.created_at) }}
            </a-descriptions-item>
            <a-descriptions-item :label="$t('page_settings_profile.label_last_login')">
              {{ fmtTime(auth.currentUser?.last_login) }}
            </a-descriptions-item>
          </a-descriptions>
        </a-card>

        <a-card :title="$t('page_settings_profile.github_section_title')" style="margin-top: 16px">
          <template v-if="!githubLinked">
            <a-alert
              :message="$t('page_settings_profile.github_unlinked_description')"
              type="info"
              show-icon
              style="margin-bottom: 16px"
            />
            <a-button type="primary" @click="bindGithub">
              {{ $t('page_settings_profile.github_link_button') }}
            </a-button>
          </template>

          <template v-else>
            <a-descriptions :column="1" bordered size="small">
              <a-descriptions-item :label="$t('page_settings_profile.github_linked_as_label')">
                <a-tag color="blue">@{{ auth.currentUser?.github_login }}</a-tag>
                <a-tag v-if="githubOnly" color="orange" style="margin-left: 8px">
                  {{ $t('page_settings_profile.github_only_account_banner') }}
                </a-tag>
              </a-descriptions-item>
            </a-descriptions>

            <div style="margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap">
              <a-button
                v-if="githubOnly"
                @click="openSetPasswordModal('proactive')"
              >
                {{ $t('page_settings_profile.github_set_password_button') }}
              </a-button>

              <a-popconfirm
                v-if="hasPassword"
                :title="$t('page_settings_profile.github_unlink_confirm_title')"
                :description="$t('page_settings_profile.github_unlink_confirm_body')"
                :ok-text="$t('page_settings_profile.github_unlink_button')"
                :cancel-text="$t('common.cancel')"
                :ok-button-props="{ loading: unlinkSubmitting, danger: true }"
                @confirm="doUnlink"
              >
                <a-button danger>{{ $t('page_settings_profile.github_unlink_button') }}</a-button>
              </a-popconfirm>
              <a-button
                v-else
                danger
                @click="openSetPasswordModal('unlink')"
              >
                {{ $t('page_settings_profile.github_unlink_button') }}
              </a-button>
            </div>

            <a-alert
              v-if="bindError"
              :message="bindError"
              type="error"
              show-icon
              closable
              style="margin-top: 16px"
              @close="bindError = null"
            />
          </template>
        </a-card>

        <!--
          Email-change section. The current address is read-only; the
          actual change is initiated through the modal so we re-collect
          ``current_password`` (proof of identity) before the backend
          mails a confirmation link to the *new* address.
        -->
        <a-card :title="$t('page_settings_profile.email_section_title')" style="margin-top: 16px">
          <a-descriptions :column="1" bordered size="small">
            <a-descriptions-item :label="$t('page_settings_profile.label_email')">
              <span>{{ auth.currentUser?.email ?? '—' }}</span>
              <a-tag v-if="auth.currentUser?.email_verified" color="green" style="margin-left: 8px">
                {{ $t('page_settings_profile.tag_verified') }}
              </a-tag>
              <a-tag v-else color="orange" style="margin-left: 8px">
                {{ $t('page_settings_profile.tag_unverified') }}
              </a-tag>
            </a-descriptions-item>
          </a-descriptions>
          <div style="margin-top: 12px">
            <a-button @click="openEmailModal">
              {{ $t('page_settings_profile.email_change_button') }}
            </a-button>
          </div>
        </a-card>

        <!--
          Password moved banner. The inline password form was removed in
          favour of the dedicated /settings/password page (the canonical
          form). Keep a banner + link so users used to the old layout
          aren't confused.
        -->
        <a-card :title="$t('page_settings_profile.card_change_password')" style="margin-top: 16px">
          <a-alert
            :message="$t('page_settings_profile.password_moved_banner')"
            type="info"
            show-icon
          >
            <template #description>
              <router-link to="/settings/password">
                {{ $t('page_settings_profile.password_moved_link') }}
              </router-link>
            </template>
          </a-alert>
        </a-card>
      </a-tab-pane>
    </a-tabs>

    <a-modal
      v-model:open="setPasswordModalOpen"
      :title="
        setPasswordModalIntent === 'unlink'
          ? $t('page_settings_profile.github_set_password_first')
          : $t('page_settings_profile.github_set_password_modal_title')
      "
      :ok-text="$t('page_settings_profile.github_set_password_submit')"
      :cancel-text="$t('common.cancel')"
      :ok-button-props="{ loading: setPasswordSubmitting }"
      :confirm-loading="setPasswordSubmitting"
      @ok="submitSetPassword"
      @cancel="setPasswordModalOpen = false"
    >
      <a-alert
        v-if="setPasswordModalIntent === 'unlink'"
        :message="$t('page_settings_profile.github_set_password_first_desc')"
        type="warning"
        show-icon
        style="margin-bottom: 16px"
      />
      <a-form layout="vertical">
        <a-form-item :label="$t('page_settings_profile.label_new_password')">
          <a-input-password v-model:value="setPasswordForm.new_password" autocomplete="new-password" />
          <div class="muted" style="font-size: 12px; margin-top: 4px">
            {{ $t('page_register.password_hint') }}
          </div>
        </a-form-item>
        <a-form-item :label="$t('page_settings_profile.label_confirm_new_password')">
          <a-input-password v-model:value="setPasswordForm.confirm" autocomplete="new-password" />
        </a-form-item>
        <a-alert
          v-if="setPasswordError"
          :message="setPasswordError"
          type="error"
          show-icon
        />
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="emailModalOpen"
      :title="$t('page_settings_profile.email_change_modal_title')"
      :ok-text="$t('page_settings_profile.email_modal_submit')"
      :cancel-text="$t('common.cancel')"
      :ok-button-props="{ loading: emailSubmitting }"
      :confirm-loading="emailSubmitting"
      @ok="submitEmailChange"
      @cancel="emailModalOpen = false"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('page_settings_profile.email_modal_new_email')">
          <a-input v-model:value="emailForm.new_email" autocomplete="email" type="email" />
        </a-form-item>
        <a-form-item :label="$t('page_settings_profile.email_modal_current_password')">
          <a-input-password v-model:value="emailForm.current_password" autocomplete="current-password" />
        </a-form-item>
        <a-alert
          v-if="emailError"
          :message="emailError"
          type="error"
          show-icon
        />
      </a-form>
    </a-modal>
  </div>
</template>
