<script setup lang="ts">
/**
 * /settings/smtp — admin form for the outbound SMTP provider.
 *
 * Backed by the existing /api/admin/email/smtp endpoint (Team Email)
 * so the legacy /admin/email/smtp page and this new home share the
 * same store.  The implementation here mirrors the legacy page with
 * lighter chrome (no breadcrumb-to-Admin-Email scaffold).
 */
import { onMounted, reactive, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { ExperimentOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons-vue';
import {
  getSmtpConfig,
  updateSmtpConfig,
  testSmtpConfig,
  type SmtpConfigIn,
} from '../../../api/email';

const { t } = useI18n();

const PASSWORD_SENTINEL = '***';

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

const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const testResult = ref<{ ok: boolean; message: string } | null>(null);

function formToPayload(): SmtpConfigIn {
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
    /* interceptor */
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  saving.value = true;
  try {
    await updateSmtpConfig(formToPayload());
    notification.success({
      message: t('settings.admin.smtp.saved_message'),
      duration: 2,
    });
    form.smtp_password = PASSWORD_SENTINEL;
  } catch {
    /* interceptor */
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
      testResult.value = { ok: true, message: t('settings.admin.smtp.test_ok') };
    } else {
      testResult.value = {
        ok: false,
        message: res.error ?? t('settings.admin.smtp.test_fail_generic'),
      };
    }
  } catch {
    testResult.value = {
      ok: false,
      message: t('settings.admin.smtp.test_fail_generic'),
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
      <a-breadcrumb-item>{{ t('nav.settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('nav.admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('settings.admin.smtp.title') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="t('settings.admin.smtp.title')" :loading="loading">
      <template #extra>
        <a-button :loading="loading" @click="load">
          <template #icon><ReloadOutlined /></template>
          {{ t('settings.admin.common.reload') }}
        </a-button>
      </template>

      <a-form layout="vertical" :model="form">
        <a-row :gutter="16">
          <a-col :xs="24" :md="16">
            <a-form-item :label="t('settings.admin.smtp.host')" required>
              <a-input v-model:value="form.smtp_host" placeholder="smtp.example.com" />
            </a-form-item>
          </a-col>
          <a-col :xs="24" :md="8">
            <a-form-item :label="t('settings.admin.smtp.port')" required>
              <a-input-number
                v-model:value="form.smtp_port"
                :min="1"
                :max="65535"
                style="width: 100%"
              />
            </a-form-item>
          </a-col>
        </a-row>

        <a-row :gutter="16">
          <a-col :xs="24" :md="12">
            <a-form-item :label="t('settings.admin.smtp.user')">
              <a-input v-model:value="form.smtp_username" autocomplete="off" />
            </a-form-item>
          </a-col>
          <a-col :xs="24" :md="12">
            <a-form-item :label="t('settings.admin.smtp.password')">
              <a-input-password
                v-model:value="form.smtp_password"
                autocomplete="new-password"
                :placeholder="t('settings.admin.common.secret_placeholder')"
              />
            </a-form-item>
          </a-col>
        </a-row>

        <a-row :gutter="16">
          <a-col :xs="24" :md="12">
            <a-form-item :label="t('settings.admin.smtp.from')" required>
              <a-input
                v-model:value="form.smtp_from_address"
                placeholder="no-reply@example.com"
              />
            </a-form-item>
          </a-col>
          <a-col :xs="24" :md="12">
            <a-form-item :label="t('settings.admin.smtp.from_name')">
              <a-input v-model:value="form.smtp_from_name" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item :label="t('settings.admin.smtp.tls')">
          <a-space direction="vertical">
            <a-checkbox v-model:checked="form.use_tls">
              {{ t('settings.admin.smtp.tls') }} (STARTTLS)
            </a-checkbox>
            <a-checkbox v-model:checked="form.use_ssl">
              {{ t('settings.admin.smtp.ssl') }} (SMTPS)
            </a-checkbox>
          </a-space>
        </a-form-item>

        <a-form-item>
          <a-switch v-model:checked="form.enabled" />
          <span class="muted" style="margin-left: 10px; font-size: 12px">
            {{ form.enabled
              ? t('settings.admin.smtp.enabled_on')
              : t('settings.admin.smtp.enabled_off') }}
          </span>
        </a-form-item>

        <a-divider style="margin: 8px 0 16px" />

        <a-space wrap>
          <a-button :loading="testing" @click="testConnection">
            <template #icon><ExperimentOutlined /></template>
            {{ t('settings.admin.smtp.test_button') }}
          </a-button>
          <a-button type="primary" :loading="saving" @click="save">
            <template #icon><SaveOutlined /></template>
            {{ t('settings.admin.common.save_button') }}
          </a-button>
        </a-space>

        <a-alert
          v-if="testResult"
          style="margin-top: 16px"
          :type="testResult.ok ? 'success' : 'error'"
          :message="testResult.ok
            ? t('settings.admin.smtp.test_ok')
            : t('settings.admin.smtp.test_fail_generic')"
          :description="testResult.message"
          show-icon
          closable
          @close="testResult = null"
        />
      </a-form>
    </a-card>
  </div>
</template>
