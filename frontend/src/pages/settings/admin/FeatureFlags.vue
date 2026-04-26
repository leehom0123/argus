<script setup lang="ts">
/**
 * /settings/feature-flags — relocated home for the legacy
 * /admin/feature-flags page.
 *
 * Backed by the same /api/admin/feature-flags endpoint so existing
 * tests and consumers keep working; we just present a single card
 * with an inline editor per flag instead of the standalone admin
 * page chrome.
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons-vue';
import { listFeatureFlags, updateFeatureFlag } from '../../../api/admin';
import type { FeatureFlag, FeatureFlagValue } from '../../../types';

type FlagType = 'bool' | 'int' | 'string';

interface FlagDraft {
  key: string;
  value: FeatureFlagValue;
  type: FlagType;
  updated_at?: string | null;
  saving: boolean;
}

const { t, te } = useI18n();
const flags = ref<FeatureFlag[]>([]);
const drafts = reactive<Record<string, FlagDraft>>({});
const loading = ref(false);

function inferType(v: FeatureFlagValue): FlagType {
  if (typeof v === 'boolean') return 'bool';
  if (typeof v === 'number') return 'int';
  return 'string';
}

function seedDraft(f: FeatureFlag): void {
  drafts[f.key] = {
    key: f.key,
    value: f.value,
    type: inferType(f.value),
    updated_at: f.updated_at ?? null,
    saving: false,
  };
}

async function fetchAll(): Promise<void> {
  loading.value = true;
  try {
    flags.value = (await listFeatureFlags()) ?? [];
    for (const k of Object.keys(drafts)) delete drafts[k];
    for (const f of flags.value) seedDraft(f);
  } catch {
    /* interceptor */
  } finally {
    loading.value = false;
  }
}

function isDirty(d: FlagDraft): boolean {
  const server = flags.value.find((f) => f.key === d.key);
  if (!server) return true;
  return d.value !== server.value;
}

async function save(d: FlagDraft): Promise<void> {
  d.saving = true;
  try {
    const updated = await updateFeatureFlag(d.key, d.value);
    notification.success({
      message: t('settings.admin.feature_flags.saved_message'),
      duration: 2,
    });
    const i = flags.value.findIndex((f) => f.key === d.key);
    if (i >= 0) flags.value[i] = updated;
    else flags.value.push(updated);
    seedDraft(updated);
  } catch {
    /* interceptor */
  } finally {
    d.saving = false;
  }
}

const draftList = computed<FlagDraft[]>(() =>
  Object.values(drafts).sort((a, b) => a.key.localeCompare(b.key)),
);

onMounted(fetchAll);
</script>

<template>
  <div class="page-container" style="max-width: 760px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ t('nav.settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('nav.admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('settings.admin.feature_flags.title') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="t('settings.admin.feature_flags.title')" :loading="loading">
      <template #extra>
        <a-button :loading="loading" @click="fetchAll">
          <template #icon><ReloadOutlined /></template>
          {{ t('settings.admin.common.reload') }}
        </a-button>
      </template>

      <a-empty v-if="!loading && draftList.length === 0" />

      <a-list :data-source="draftList" :split="true">
        <template #renderItem="{ item }">
          <a-list-item>
            <div style="width: 100%">
              <div style="display: flex; align-items: center; justify-content: space-between">
                <div>
                  <strong>
                    {{ t(`settings.admin.feature_flags.flags.${item.key}.label`, item.key) }}
                  </strong>
                  <span class="muted" style="font-size: 11px; margin-left: 8px; font-family: monospace">
                    {{ item.key }}
                  </span>
                  <div
                    v-if="te(`settings.admin.feature_flags.flags.${item.key}.desc`)"
                    class="muted"
                    style="font-size: 12px; margin-top: 2px"
                  >
                    {{ t(`settings.admin.feature_flags.flags.${item.key}.desc`) }}
                  </div>
                </div>
                <a-tag v-if="isDirty(item)" color="orange" style="font-size: 11px">
                  {{ t('settings.admin.common.unsaved', 'unsaved') }}
                </a-tag>
              </div>

              <div style="margin-top: 8px; display: flex; gap: 12px; align-items: center">
                <a-switch
                  v-if="item.type === 'bool'"
                  v-model:checked="item.value as boolean"
                />
                <a-input-number
                  v-else-if="item.type === 'int'"
                  v-model:value="item.value as number"
                  style="width: 200px"
                />
                <a-input
                  v-else
                  v-model:value="item.value as string"
                  style="max-width: 360px"
                />

                <a-button
                  type="primary"
                  size="small"
                  :loading="item.saving"
                  :disabled="!isDirty(item)"
                  @click="save(item)"
                >
                  <template #icon><SaveOutlined /></template>
                  {{ t('settings.admin.common.save_button') }}
                </a-button>
              </div>
            </div>
          </a-list-item>
        </template>
      </a-list>
    </a-card>
  </div>
</template>
