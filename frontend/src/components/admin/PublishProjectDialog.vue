<script setup lang="ts">
/**
 * Admin modal for toggling a project's public-demo visibility. Opens
 * from the ProjectDetail header when the current user is admin.
 *
 * Two states mirror the backend:
 *   - currently private → "Publish" primary button
 *   - currently public  → description edit + Unpublish (danger) action
 */

import { computed, ref, watch } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { CopyOutlined } from '@ant-design/icons-vue';
import { publishProject, unpublishProject } from '../../api/admin';

const props = defineProps<{
  open: boolean;
  project: string;
  isPublic: boolean | null;
  currentDescription: string | null;
}>();
const emit = defineEmits<{
  (e: 'update:open', v: boolean): void;
  (e: 'changed'): void;
}>();

const { t } = useI18n();
const description = ref<string>(props.currentDescription ?? '');
const loading = ref(false);
const makePublic = ref<boolean>(props.isPublic ?? false);

watch(
  () => [props.open, props.currentDescription, props.isPublic] as const,
  ([open, desc, pub]) => {
    if (open) {
      description.value = (desc as string | null) ?? '';
      makePublic.value = (pub as boolean | null) ?? false;
    }
  },
);

const demoUrl = computed(() => {
  if (typeof window === 'undefined') return '';
  return `${window.location.origin}/demo/${encodeURIComponent(props.project)}`;
});

async function onOk(): Promise<void> {
  loading.value = true;
  try {
    if (makePublic.value) {
      await publishProject(
        props.project,
        description.value.trim() || null,
      );
      notification.success({
        message: t('page_project_detail.publish_success'),
        description: demoUrl.value,
        duration: 5,
      });
    } else {
      await unpublishProject(props.project);
      notification.success({
        message: t('page_project_detail.unpublish_success'),
        duration: 3,
      });
    }
    emit('changed');
    emit('update:open', false);
  } catch {
    // interceptor notifies
  } finally {
    loading.value = false;
  }
}

async function copyLink(): Promise<void> {
  try {
    await navigator.clipboard.writeText(demoUrl.value);
    notification.success({
      message: t('page_project_detail.copied'),
      duration: 2,
    });
  } catch {
    // noop — clipboard may be unavailable
  }
}
</script>

<template>
  <a-modal
    :open="open"
    :title="t('page_project_detail.publish_dialog_title')"
    :confirm-loading="loading"
    @update:open="(v: boolean) => emit('update:open', v)"
    @ok="onOk"
    @cancel="emit('update:open', false)"
  >
    <p class="muted" style="font-size: 13px; margin-bottom: 14px">
      {{ t('page_project_detail.publish_dialog_body') }}
    </p>

    <a-form layout="vertical">
      <a-form-item>
        <a-checkbox v-model:checked="makePublic">
          {{ t('page_project_detail.publish_make_public') }}
        </a-checkbox>
      </a-form-item>

      <a-form-item
        :label="t('page_project_detail.publish_description_label')"
        :help="t('page_project_detail.publish_description_hint')"
      >
        <a-textarea
          v-model:value="description"
          :maxlength="500"
          show-count
          :rows="4"
          :disabled="!makePublic"
        />
      </a-form-item>

      <a-form-item v-if="makePublic">
        <a-input-group compact>
          <a-input
            :value="demoUrl"
            readonly
            style="width: calc(100% - 40px)"
          />
          <a-tooltip :title="t('page_project_detail.copy_link')">
            <a-button @click="copyLink">
              <template #icon><CopyOutlined /></template>
            </a-button>
          </a-tooltip>
        </a-input-group>
      </a-form-item>
    </a-form>
  </a-modal>
</template>
