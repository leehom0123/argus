<script setup lang="ts">
/**
 * /settings/oauth-github — admin form for the GitHub OAuth provider.
 *
 * Wraps GET/PUT /api/admin/system-config/oauth.  The encrypted client
 * secret arrives masked as ``"***"``; if the admin leaves the field
 * untouched, we PUT the mask back and the backend preserves the
 * stored ciphertext.
 */
import { onMounted, reactive, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons-vue';
import {
  getSystemConfigGroup,
  putSystemConfig,
  type SystemConfigItem,
} from '../../../api/admin';

const { t } = useI18n();

interface FormModel {
  github_enabled: boolean;
  github_client_id: string;
  github_client_secret: string;
  github_callback: string;
}

const form = reactive<FormModel>({
  github_enabled: false,
  github_client_id: '',
  github_client_secret: '',
  github_callback: '',
});

const items = ref<Record<string, SystemConfigItem>>({});
const loading = ref(false);
const saving = ref(false);

function indexItems(rows: SystemConfigItem[]): Record<string, SystemConfigItem> {
  const out: Record<string, SystemConfigItem> = {};
  for (const r of rows) out[r.key] = r;
  return out;
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const rows = await getSystemConfigGroup('oauth');
    items.value = indexItems(rows);
    form.github_enabled = Boolean(items.value['github_enabled']?.value ?? false);
    form.github_client_id = String(items.value['github_client_id']?.value ?? '');
    form.github_client_secret = String(
      items.value['github_client_secret']?.value ?? '',
    );
    form.github_callback = String(items.value['github_callback']?.value ?? '');
  } catch {
    /* axios interceptor toasts */
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  saving.value = true;
  try {
    await putSystemConfig('oauth', 'github_enabled', form.github_enabled);
    await putSystemConfig('oauth', 'github_client_id', form.github_client_id);
    // Only push the secret when it has been edited away from the mask.
    if (form.github_client_secret && form.github_client_secret !== '***') {
      await putSystemConfig(
        'oauth',
        'github_client_secret',
        form.github_client_secret,
      );
    }
    if (form.github_callback) {
      await putSystemConfig('oauth', 'github_callback', form.github_callback);
    }
    notification.success({
      message: t('settings.admin.oauth_github.saved_message'),
      duration: 2,
    });
    await load();
  } catch {
    /* interceptor */
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page-container" style="max-width: 760px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ t('nav.settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('nav.admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('settings.admin.oauth_github.title') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="t('settings.admin.oauth_github.title')" :loading="loading">
      <template #extra>
        <a-button :loading="loading" @click="load">
          <template #icon><ReloadOutlined /></template>
          {{ t('settings.admin.common.reload') }}
        </a-button>
      </template>

      <a-form layout="vertical" :model="form">
        <a-form-item :label="t('settings.admin.oauth_github.enabled')">
          <a-switch v-model:checked="form.github_enabled" />
        </a-form-item>
        <a-form-item :label="t('settings.admin.oauth_github.client_id')">
          <a-input v-model:value="form.github_client_id" autocomplete="off" />
        </a-form-item>
        <a-form-item :label="t('settings.admin.oauth_github.client_secret')">
          <a-input-password
            v-model:value="form.github_client_secret"
            autocomplete="new-password"
            :placeholder="t('settings.admin.common.secret_placeholder')"
          />
          <div class="muted" style="font-size: 11px; margin-top: 4px">
            {{ t('settings.admin.common.secret_help') }}
          </div>
        </a-form-item>
        <a-form-item :label="t('settings.admin.oauth_github.callback')">
          <a-input
            v-model:value="form.github_callback"
            autocomplete="off"
            placeholder="https://argus.example.com/api/auth/oauth/github/callback"
          />
        </a-form-item>

        <a-button type="primary" :loading="saving" @click="save">
          <template #icon><SaveOutlined /></template>
          {{ t('settings.admin.common.save_button') }}
        </a-button>
      </a-form>
    </a-card>
  </div>
</template>
