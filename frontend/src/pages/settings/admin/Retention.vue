<script setup lang="ts">
/**
 * /settings/retention — admin form for the data-retention sweep caps.
 *
 * Each tunable is a "days" knob; setting it to 0 disables that rule.
 * The retention loop re-reads ``system_config`` on every iteration so
 * a change here takes effect within the next sweep window
 * (``ARGUS_RETENTION_SWEEP_MINUTES``, default 60 min).
 */
import { onMounted, reactive, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { ReloadOutlined, SaveOutlined, ThunderboltOutlined } from '@ant-design/icons-vue';
import {
  getSystemConfigGroup,
  putSystemConfig,
  type SystemConfigItem,
} from '../../../api/admin';
import { http } from '../../../api/client';

const { t } = useI18n();

interface FormModel {
  snapshot_days: number;
  log_line_days: number;
  job_epoch_days: number;
  event_other_days: number;
  demo_data_days: number;
}

const form = reactive<FormModel>({
  snapshot_days: 7,
  log_line_days: 14,
  job_epoch_days: 30,
  event_other_days: 90,
  demo_data_days: 1,
});

const items = ref<Record<string, SystemConfigItem>>({});
const loading = ref(false);
const saving = ref(false);
const sweeping = ref(false);

function indexItems(rows: SystemConfigItem[]): Record<string, SystemConfigItem> {
  const out: Record<string, SystemConfigItem> = {};
  for (const r of rows) out[r.key] = r;
  return out;
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const rows = await getSystemConfigGroup('retention');
    items.value = indexItems(rows);
    for (const k of Object.keys(form) as (keyof FormModel)[]) {
      const raw = items.value[k]?.value;
      const n = Number(raw);
      if (!Number.isNaN(n)) form[k] = n;
    }
  } catch {
    /* interceptor */
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  saving.value = true;
  try {
    for (const k of Object.keys(form) as (keyof FormModel)[]) {
      await putSystemConfig('retention', k, Number(form[k]));
    }
    notification.success({
      message: t('settings.admin.retention.saved_message'),
      duration: 2,
    });
    await load();
  } catch {
    /* interceptor */
  } finally {
    saving.value = false;
  }
}

async function runSweep(): Promise<void> {
  sweeping.value = true;
  try {
    const { data } = await http.post<Record<string, number>>('/admin/retention/sweep');
    const total = Object.values(data ?? {}).reduce(
      (a: number, b) => a + (Number(b) > 0 ? Number(b) : 0),
      0,
    );
    notification.success({
      message: t('settings.admin.retention.sweep_done', { n: total }),
      duration: 3,
    });
  } catch {
    /* interceptor */
  } finally {
    sweeping.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page-container" style="max-width: 720px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ t('nav.settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('nav.admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('settings.admin.retention.title') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="t('settings.admin.retention.title')" :loading="loading">
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
        :message="t('settings.admin.retention.hint_title')"
        :description="t('settings.admin.retention.hint_desc')"
      />

      <a-form layout="vertical" :model="form">
        <a-form-item :label="t('settings.admin.retention.snapshot_days')">
          <a-input-number v-model:value="form.snapshot_days" :min="0" style="width: 100%" />
        </a-form-item>
        <a-form-item :label="t('settings.admin.retention.log_days')">
          <a-input-number v-model:value="form.log_line_days" :min="0" style="width: 100%" />
        </a-form-item>
        <a-form-item :label="t('settings.admin.retention.job_epoch_days')">
          <a-input-number v-model:value="form.job_epoch_days" :min="0" style="width: 100%" />
        </a-form-item>
        <a-form-item :label="t('settings.admin.retention.event_days')">
          <a-input-number v-model:value="form.event_other_days" :min="0" style="width: 100%" />
        </a-form-item>
        <a-form-item :label="t('settings.admin.retention.demo_data_days')">
          <a-input-number v-model:value="form.demo_data_days" :min="0" style="width: 100%" />
        </a-form-item>

        <a-divider style="margin: 8px 0 16px" />

        <a-space>
          <a-button type="primary" :loading="saving" @click="save">
            <template #icon><SaveOutlined /></template>
            {{ t('settings.admin.common.save_button') }}
          </a-button>
          <a-button :loading="sweeping" @click="runSweep">
            <template #icon><ThunderboltOutlined /></template>
            {{ t('settings.admin.retention.run_sweep_now') }}
          </a-button>
        </a-space>
      </a-form>
    </a-card>
  </div>
</template>
