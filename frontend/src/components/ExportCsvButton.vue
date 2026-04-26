<script setup lang="ts">
import { ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { DownloadOutlined } from '@ant-design/icons-vue';
import { notification } from 'ant-design-vue';

/**
 * Thin wrapper around any of the CSV export helpers in `api/exports.ts`.
 * The parent passes a zero-arg async `handler` (usually a closure that
 * captures the batch id / project name / batch-ids list).
 */
const { t } = useI18n();
const props = withDefaults(
  defineProps<{
    handler: () => Promise<void>;
    label?: string;
    size?: 'small' | 'middle' | 'large';
    type?: 'primary' | 'default' | 'text' | 'link' | 'dashed' | 'ghost';
    /** Override the toast success message. */
    successMessage?: string | null;
    disabled?: boolean;
  }>(),
  {
    label: '',
    size: 'small',
    type: 'default',
    successMessage: null,
    disabled: false,
  },
);

const loading = ref(false);

async function click(ev: MouseEvent) {
  ev.stopPropagation();
  ev.preventDefault();
  if (loading.value || props.disabled) return;
  loading.value = true;
  try {
    await props.handler();
    const msg = props.successMessage || t('component_export_csv_button.download_started');
    if (msg) {
      notification.success({ message: msg, duration: 2 });
    }
  } catch (e) {
    notification.error({
      message: t('component_export_csv_button.export_failed'),
      description: (e as Error).message || 'Unknown error; check network tab.',
      duration: 3,
    });
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <a-button
    :type="type"
    :size="size"
    :loading="loading"
    :disabled="disabled"
    @click="click"
  >
    <template #icon><DownloadOutlined /></template>
    {{ label || $t('component_export_csv_button.export_csv') }}
  </a-button>
</template>
