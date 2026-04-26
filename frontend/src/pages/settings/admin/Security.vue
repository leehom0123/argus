<script setup lang="ts">
/**
 * /settings/security — admin form for security-related runtime knobs.
 *
 * Today this page exposes a single action: "Rotate JWT secret". Once
 * rotated, the previous secret keeps verifying already-issued tokens
 * for the configured grace window (default 24h) so nobody is force-
 * logged-out by the rotation. We surface the last-rotation timestamp
 * + a live countdown for the grace window so admins can see when the
 * old secret will stop being honoured.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import axios from 'axios';
import { Modal, notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import {
  ReloadOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue';
import {
  getJwtRotationStatus,
  rotateJwtSecret,
  type JwtRotationStatus,
} from '../../../api/admin';

const { t, d } = useI18n();

const status = ref<JwtRotationStatus | null>(null);
const loading = ref(false);
const rotating = ref(false);

// Anti-double-rotate cooldown driven by a 429 response. Backend enforces
// a 60s window; we mirror it in the UI by disabling the rotate button
// until ``cooldownUntil`` passes. Drives the toast countdown text too.
const cooldownUntil = ref<number>(0);

// Tick every second so the countdown updates without re-fetching.
const now = ref(Date.now());
let tickHandle: ReturnType<typeof setInterval> | null = null;

const cooldownRemainingSeconds = computed<number>(() => {
  if (!cooldownUntil.value) return 0;
  return Math.max(0, Math.ceil((cooldownUntil.value - now.value) / 1000));
});

const rotateDisabled = computed<boolean>(() => cooldownRemainingSeconds.value > 0);

const rotatedAtLabel = computed<string>(() => {
  const ts = status.value?.rotated_at;
  if (!ts) return t('settings.admin.security.rotate.never_rotated');
  // Render in the user's locale; ``d`` from vue-i18n handles both
  // en-US (en) and zh-CN (cn) date-time formatters.
  try {
    return d(new Date(ts), 'long' as never);
  } catch {
    return ts;
  }
});

/**
 * Time left in the grace window, in seconds. Negative when no
 * previous secret is being held (the field is nulled out by the
 * sweeper after the grace elapses).
 */
const previousRemainingSeconds = computed<number>(() => {
  if (!status.value?.has_previous || !status.value?.previous_expires_at) {
    return -1;
  }
  const exp = Date.parse(status.value.previous_expires_at);
  if (Number.isNaN(exp)) return -1;
  return Math.max(0, Math.floor((exp - now.value) / 1000));
});

const previousCountdownLabel = computed<string>(() => {
  const s = previousRemainingSeconds.value;
  if (s < 0) return t('settings.admin.security.rotate.no_previous');
  if (s === 0) return t('settings.admin.security.rotate.previous_expired');
  const hours = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  // 25h:14m:09s style — short enough to fit in the card subtitle.
  return t('settings.admin.security.rotate.previous_remaining', {
    hours: String(hours),
    mins: String(mins).padStart(2, '0'),
    secs: String(secs).padStart(2, '0'),
  });
});

async function load(): Promise<void> {
  loading.value = true;
  try {
    status.value = await getJwtRotationStatus();
  } catch {
    /* interceptor toasts the 4xx/5xx */
  } finally {
    loading.value = false;
  }
}

async function performRotate(): Promise<void> {
  rotating.value = true;
  try {
    const res = await rotateJwtSecret();
    notification.success({
      message: t('settings.admin.security.rotate.success_title'),
      description: t('settings.admin.security.rotate.success_desc', {
        rotated_at: res.rotated_at,
      }),
      duration: 4,
    });
    await load();
  } catch (err: unknown) {
    // Anti-double-rotate guard: backend returns 429 + Retry-After when a
    // second rotation lands inside the 60s cooldown. The global axios
    // interceptor doesn't toast 429 (it's not a generic error path), so
    // we surface a focused warning + lock the button until the window
    // closes. Header is the source of truth; body retry_after is a
    // fallback for callers that strip headers.
    if (axios.isAxiosError(err) && err.response?.status === 429) {
      const headerVal = err.response.headers?.['retry-after'];
      const bodyVal =
        (err.response.data as { detail?: { retry_after?: number } } | undefined)?.detail?.retry_after;
      const seconds = Math.max(1, Number(headerVal) || Number(bodyVal) || 60);
      cooldownUntil.value = Date.now() + seconds * 1000;
      notification.warning({
        message: t('settings.admin.security.rotate.cooldown_title'),
        description: t('settings.admin.security.rotate.cooldown_desc', { seconds: String(seconds) }),
        duration: 4,
      });
    }
    /* other failures already toasted by the interceptor */
  } finally {
    rotating.value = false;
  }
}

function confirmRotate(): void {
  Modal.confirm({
    title: t('settings.admin.security.rotate.confirm_title'),
    content: t('settings.admin.security.rotate.confirm_body'),
    okText: t('settings.admin.security.rotate.confirm_ok'),
    cancelText: t('settings.admin.security.rotate.confirm_cancel'),
    okButtonProps: { danger: true },
    onOk: () => performRotate(),
  });
}

onMounted(() => {
  void load();
  tickHandle = setInterval(() => {
    now.value = Date.now();
  }, 1000);
});

onBeforeUnmount(() => {
  if (tickHandle !== null) clearInterval(tickHandle);
});
</script>

<template>
  <div class="page-container" style="max-width: 720px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ t('nav.settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('nav.admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('settings.admin.security.title') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="t('settings.admin.security.title')" :loading="loading">
      <template #extra>
        <a-button :loading="loading" @click="load">
          <template #icon><ReloadOutlined /></template>
          {{ t('settings.admin.common.reload') }}
        </a-button>
      </template>

      <a-alert
        type="info"
        show-icon
        style="margin-bottom: 16px"
        :message="t('settings.admin.security.rotate.hint_title')"
        :description="t('settings.admin.security.rotate.hint_desc')"
      />

      <a-descriptions
        bordered
        size="small"
        :column="1"
        style="margin-bottom: 20px"
      >
        <a-descriptions-item :label="t('settings.admin.security.rotate.last_rotated')">
          <span data-testid="rotated-at">{{ rotatedAtLabel }}</span>
        </a-descriptions-item>
        <a-descriptions-item :label="t('settings.admin.security.rotate.previous_label')">
          <span data-testid="previous-countdown">{{ previousCountdownLabel }}</span>
        </a-descriptions-item>
      </a-descriptions>

      <a-space>
        <a-button
          type="primary"
          danger
          :loading="rotating"
          :disabled="rotateDisabled"
          data-testid="rotate-jwt-btn"
          @click="confirmRotate"
        >
          <template #icon><ThunderboltOutlined /></template>
          {{
            rotateDisabled
              ? t('settings.admin.security.rotate.cooldown_button', {
                  seconds: String(cooldownRemainingSeconds),
                })
              : t('settings.admin.security.rotate.button')
          }}
        </a-button>
        <SafetyCertificateOutlined style="color: #888" />
        <span style="color: #888; font-size: 13px">
          {{ t('settings.admin.security.rotate.no_logout_note') }}
        </span>
      </a-space>
    </a-card>
  </div>
</template>
