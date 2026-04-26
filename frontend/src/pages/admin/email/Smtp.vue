<script setup lang="ts">
/**
 * /admin/email/smtp — SMTP transport configuration.
 *
 * Password handling deserves care:
 *   - The backend NEVER returns the real ``smtp_password``. Every GET
 *     substitutes the literal sentinel ``"***"``.
 *   - If the admin leaves that sentinel unchanged, the PUT echoes it back
 *     and the backend preserves the stored password.
 *   - "Test connection" sends the current form values — so an admin who
 *     just typed a new password can verify BEFORE saving.
 */

import { onMounted, reactive, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { ReloadOutlined, SaveOutlined, ExperimentOutlined } from '@ant-design/icons-vue';
import {
  getSmtpConfig,
  updateSmtpConfig,
  testSmtpConfig,
  type SmtpConfigIn,
} from '../../../api/email';

const { t } = useI18n();

const PASSWORD_SENTINEL = '***';

const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const testResult = ref<{ ok: boolean; message: string } | null>(null);

// Reactive form model — a narrower version of ``SmtpConfigIn`` that
// substitutes empty strings for null (so ``a-input`` accepts the binding).
// Null / empty-string round-trip is fine on the backend: Pydantic treats
// ``""`` as valid for Optional[str].
interface SmtpFormModel {
  enabled: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_username: string;
  smtp_password: string;
  smtp_from_address: string;
  smtp_from_name: string;
  use_tls: boolean;
  use_ssl: boolean;
}

const form = reactive<SmtpFormModel>({
  enabled: false,
  smtp_host: '',
  smtp_port: 587,
  smtp_username: '',
  smtp_password: '',
  smtp_from_address: '',
  smtp_from_name: '',
  use_tls: true,
  use_ssl: false,
});

function formToPayload(): SmtpConfigIn {
  // Strings are sent as-is; empty string is fine on the backend per the
  // shape documented in ``schemas/email.py``.
  return { ...form };
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const cfg = await getSmtpConfig();
    form.enabled = cfg.enabled;
    form.smtp_host = cfg.smtp_host ?? '';
    form.smtp_port = cfg.smtp_port ?? 587;
    form.smtp_username = cfg.smtp_username ?? '';
    form.smtp_password = cfg.smtp_password ?? PASSWORD_SENTINEL;
    form.smtp_from_address = cfg.smtp_from_address ?? '';
    form.smtp_from_name = cfg.smtp_from_name ?? '';
    form.use_tls = cfg.use_tls;
    form.use_ssl = cfg.use_ssl;
  } catch {
    // axios interceptor toasts; leave form at defaults
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  saving.value = true;
  try {
    await updateSmtpConfig(formToPayload());
    notification.success({
      message: t('page_admin_email_smtp.saved'),
      duration: 2,
    });
    // Re-mask the password after save so it isn't left visible in memory.
    form.smtp_password = PASSWORD_SENTINEL;
  } catch {
    // interceptor notified
  } finally {
    saving.value = false;
  }
}

async function testConnection(): Promise<void> {
  testing.value = true;
  testResult.value = null;
  try {
    const res = await testSmtpConfig(formToPayload());
    if (res.ok) {
      testResult.value = { ok: true, message: t('page_admin_email_smtp.test_ok') };
    } else {
      testResult.value = {
        ok: false,
        message: res.error ?? t('page_admin_email_smtp.test_fail_generic'),
      };
    }
  } catch {
    testResult.value = {
      ok: false,
      message: t('page_admin_email_smtp.test_fail_generic'),
    };
  } finally {
    testing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page-container" style="max-width: 800px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ t('page_admin_email_smtp.breadcrumb_admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('page_admin_email_smtp.breadcrumb_email') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('page_admin_email_smtp.breadcrumb_smtp') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="t('page_admin_email_smtp.card_title')" :loading="loading">
      <template #extra>
        <a-button :loading="loading" @click="load">
          <template #icon><ReloadOutlined /></template>
          {{ t('page_admin_email_smtp.btn_refresh') }}
        </a-button>
      </template>

      <a-alert
        type="info"
        show-icon
        :message="t('page_admin_email_smtp.hint_title')"
        :description="t('page_admin_email_smtp.hint_desc')"
        style="margin-bottom: 16px"
      />

      <a-form layout="vertical" :model="form">
        <a-row :gutter="16">
          <a-col :xs="24" :md="16">
            <a-form-item :label="t('page_admin_email_smtp.field_host')" required>
              <a-input
                v-model:value="form.smtp_host"
                placeholder="smtp.example.com"
                autocomplete="off"
              />
            </a-form-item>
          </a-col>
          <a-col :xs="24" :md="8">
            <a-form-item :label="t('page_admin_email_smtp.field_port')" required>
              <a-input-number
                v-model:value="form.smtp_port"
                :min="1"
                :max="65535"
                :step="1"
                style="width: 100%"
              />
            </a-form-item>
          </a-col>
        </a-row>

        <a-row :gutter="16">
          <a-col :xs="24" :md="12">
            <a-form-item :label="t('page_admin_email_smtp.field_username')">
              <a-input
                v-model:value="form.smtp_username"
                autocomplete="off"
                placeholder="user@example.com"
              />
            </a-form-item>
          </a-col>
          <a-col :xs="24" :md="12">
            <a-form-item :label="t('page_admin_email_smtp.field_password')">
              <a-input-password
                v-model:value="form.smtp_password"
                autocomplete="new-password"
                :placeholder="t('page_admin_email_smtp.password_placeholder')"
              />
              <div class="muted" style="font-size: 11px; margin-top: 4px">
                {{ t('page_admin_email_smtp.password_help') }}
              </div>
            </a-form-item>
          </a-col>
        </a-row>

        <a-row :gutter="16">
          <a-col :xs="24" :md="12">
            <a-form-item :label="t('page_admin_email_smtp.field_from_address')" required>
              <a-input
                v-model:value="form.smtp_from_address"
                placeholder="no-reply@example.com"
                autocomplete="off"
              />
            </a-form-item>
          </a-col>
          <a-col :xs="24" :md="12">
            <a-form-item :label="t('page_admin_email_smtp.field_from_name')">
              <a-input
                v-model:value="form.smtp_from_name"
                placeholder="Experiment Monitor"
                autocomplete="off"
              />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item>
          <a-space direction="vertical" style="width: 100%">
            <a-checkbox v-model:checked="form.use_tls">
              {{ t('page_admin_email_smtp.field_use_tls') }}
              <span class="muted" style="font-size: 11px; margin-left: 6px">
                {{ t('page_admin_email_smtp.field_use_tls_hint') }}
              </span>
            </a-checkbox>
            <a-checkbox v-model:checked="form.use_ssl">
              {{ t('page_admin_email_smtp.field_use_ssl') }}
              <span class="muted" style="font-size: 11px; margin-left: 6px">
                {{ t('page_admin_email_smtp.field_use_ssl_hint') }}
              </span>
            </a-checkbox>
          </a-space>
        </a-form-item>

        <a-form-item :label="t('page_admin_email_smtp.field_enabled')">
          <a-switch v-model:checked="form.enabled" />
          <span class="muted" style="margin-left: 10px; font-size: 12px">
            {{ form.enabled
              ? t('page_admin_email_smtp.enabled_on')
              : t('page_admin_email_smtp.enabled_off') }}
          </span>
        </a-form-item>

        <a-divider style="margin: 8px 0 16px" />

        <a-space wrap>
          <a-button :loading="testing" @click="testConnection">
            <template #icon><ExperimentOutlined /></template>
            {{ t('page_admin_email_smtp.btn_test') }}
          </a-button>
          <a-button type="primary" :loading="saving" @click="save">
            <template #icon><SaveOutlined /></template>
            {{ t('page_admin_email_smtp.btn_save') }}
          </a-button>
        </a-space>

        <a-alert
          v-if="testResult"
          style="margin-top: 16px"
          :type="testResult.ok ? 'success' : 'error'"
          :message="testResult.ok
            ? t('page_admin_email_smtp.test_result_ok')
            : t('page_admin_email_smtp.test_result_fail')"
          :description="testResult.message"
          show-icon
          closable
          @close="testResult = null"
        />
      </a-form>
    </a-card>
  </div>
</template>
