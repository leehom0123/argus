<script setup lang="ts">
/**
 * /settings/demo-project — single switch that flips
 * ``system_config.demo.enabled``.
 *
 * Lifted from /admin/projects (publish toggle only).  The full
 * project-management UI stays at /admin/projects; this page is the
 * one knob admins toggle most often (demo-host visible to anonymous
 * /demo visitors yes / no).
 */
import { onMounted, reactive, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons-vue';
import {
  getSystemConfigGroup,
  putSystemConfig,
} from '../../../api/admin';

const { t } = useI18n();

const form = reactive({
  enabled: false,
});
const loading = ref(false);
const saving = ref(false);

async function load(): Promise<void> {
  loading.value = true;
  try {
    const rows = await getSystemConfigGroup('demo');
    const row = rows.find((r) => r.key === 'enabled');
    form.enabled = Boolean(row?.value ?? false);
  } catch {
    /* interceptor */
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  saving.value = true;
  try {
    await putSystemConfig('demo', 'enabled', form.enabled);
    notification.success({
      message: t('settings.admin.demo_project.saved_message'),
      duration: 2,
    });
  } catch {
    /* interceptor */
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page-container" style="max-width: 640px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ t('nav.settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('nav.admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('settings.admin.demo_project.title') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="t('settings.admin.demo_project.title')" :loading="loading">
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
        :message="t('settings.admin.demo_project.hint_title')"
        :description="t('settings.admin.demo_project.hint_desc')"
      />

      <a-form layout="vertical">
        <a-form-item :label="t('settings.admin.demo_project.enabled_label')">
          <a-switch v-model:checked="form.enabled" />
        </a-form-item>

        <a-button type="primary" :loading="saving" @click="save">
          <template #icon><SaveOutlined /></template>
          {{ t('settings.admin.common.save_button') }}
        </a-button>
      </a-form>
    </a-card>
  </div>
</template>
