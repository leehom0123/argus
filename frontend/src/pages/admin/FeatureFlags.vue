<script setup lang="ts">
/**
 * /admin/feature-flags — one card per flag.
 */

import { computed, onMounted, reactive, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { PlusOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons-vue';
import { listFeatureFlags, updateFeatureFlag } from '../../api/admin';
import type { FeatureFlag, FeatureFlagValue } from '../../types';
import { fmtTime, fmtRelative } from '../../utils/format';

type FlagType = 'bool' | 'int' | 'string';

interface FlagDraft {
  key: string;
  value: FeatureFlagValue;
  type: FlagType;
  updated_at?: string | null;
  saving: boolean;
}

const { t } = useI18n();
const flags = ref<FeatureFlag[]>([]);
const drafts = reactive<Record<string, FlagDraft>>({});
const loading = ref(false);

const FLAG_HINTS: Record<string, string> = {
  registration_open:
    'When off, the public /register endpoint rejects new signups with 403.',
  stalled_threshold_sec:
    'Jobs with no events for this many seconds are marked "stalled" in the UI.',
  email_verification_required:
    'When on, users cannot log in until they click the link in their verification email.',
};

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
    // interceptor notifies
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
      message: t('page_admin_feature_flags.saved'),
      duration: 2,
    });
    const idx = flags.value.findIndex((f) => f.key === d.key);
    if (idx >= 0) {
      flags.value[idx] = updated;
    } else {
      flags.value.push(updated);
    }
    d.updated_at = updated.updated_at ?? new Date().toISOString();
  } catch {
    // interceptor notified; leave draft as-is so the admin can retry
  } finally {
    d.saving = false;
  }
}

function reset(d: FlagDraft): void {
  const server = flags.value.find((f) => f.key === d.key);
  if (server) d.value = server.value;
}

const hasFlags = computed(() => flags.value.length > 0);

const knownKeys: Array<{ key: string; type: FlagType; hint: string }> = [
  { key: 'registration_open', type: 'bool', hint: FLAG_HINTS.registration_open },
  {
    key: 'stalled_threshold_sec',
    type: 'int',
    hint: FLAG_HINTS.stalled_threshold_sec,
  },
  {
    key: 'email_verification_required',
    type: 'bool',
    hint: FLAG_HINTS.email_verification_required,
  },
];

onMounted(fetchAll);
</script>

<template>
  <div class="page-container" style="max-width: 960px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_admin_feature_flags.breadcrumb_admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_admin_feature_flags.breadcrumb_flags') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="$t('page_admin_feature_flags.card_title')">
      <template #extra>
        <a-button :loading="loading" @click="fetchAll">
          <template #icon><ReloadOutlined /></template>
          {{ $t('page_admin_feature_flags.btn_refresh') }}
        </a-button>
      </template>

      <a-alert
        type="info"
        show-icon
        :message="$t('page_admin_feature_flags.alert_msg')"
        :description="$t('page_admin_feature_flags.alert_desc')"
        style="margin-bottom: 16px"
      />

      <!-- Flag cards ---------------------------------------------------- -->
      <a-row v-if="hasFlags" :gutter="[16, 16]">
        <a-col v-for="d in Object.values(drafts)" :key="d.key" :xs="24" :md="12">
          <a-card size="small" :bordered="true">
            <template #title>
              <span style="font-family: ui-monospace, monospace">{{ d.key }}</span>
              <a-tag
                :color="d.type === 'bool' ? 'green' : d.type === 'int' ? 'geekblue' : 'default'"
                style="margin-left: 8px"
              >
                {{ d.type }}
              </a-tag>
            </template>

            <div v-if="FLAG_HINTS[d.key]" class="muted" style="font-size: 12px; margin-bottom: 10px">
              {{ FLAG_HINTS[d.key] }}
            </div>

            <!-- Bool -->
            <div v-if="d.type === 'bool'">
              <a-switch
                :checked="!!d.value"
                @change="(v: boolean | string | number) => (d.value = !!v)"
              />
              <span class="muted" style="margin-left: 10px; font-size: 12px">
                {{ d.value ? $t('page_admin_feature_flags.enabled') : $t('page_admin_feature_flags.disabled') }}
              </span>
            </div>

            <!-- Int -->
            <div v-else-if="d.type === 'int'">
              <a-input-number
                :value="typeof d.value === 'number' ? d.value : Number(d.value ?? 0)"
                :min="0"
                :step="1"
                style="width: 180px"
                @change="(v: number | string | null) => (d.value = Number(v ?? 0))"
              />
            </div>

            <!-- String / unknown -->
            <div v-else>
              <a-input
                :value="d.value == null ? '' : String(d.value)"
                allow-clear
                @update:value="(v: string) => (d.value = v)"
              />
            </div>

            <div
              style="
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-top: 14px;
                font-size: 11px;
              "
            >
              <span class="muted">
                <template v-if="d.updated_at">
                  {{ $t('page_admin_feature_flags.updated', { time: fmtRelative(d.updated_at) }) }}
                  <a-tooltip :title="fmtTime(d.updated_at)">
                    <span style="margin-left: 4px; cursor: help">ⓘ</span>
                  </a-tooltip>
                </template>
                <template v-else>{{ $t('page_admin_feature_flags.never_saved') }}</template>
              </span>

              <a-space>
                <a-button
                  v-if="isDirty(d)"
                  size="small"
                  @click="reset(d)"
                >
                  {{ $t('page_admin_feature_flags.btn_reset') }}
                </a-button>
                <a-button
                  type="primary"
                  size="small"
                  :disabled="!isDirty(d)"
                  :loading="d.saving"
                  @click="save(d)"
                >
                  <template #icon><SaveOutlined /></template>
                  {{ $t('page_admin_feature_flags.btn_save') }}
                </a-button>
              </a-space>
            </div>
          </a-card>
        </a-col>
      </a-row>

      <!-- Empty state --------------------------------------------------- -->
      <div v-else-if="!loading" class="empty-wrap">
        <PlusOutlined style="font-size: 28px; display: block; margin-bottom: 10px" />
        <div style="font-size: 15px; margin-bottom: 6px">{{ $t('page_admin_feature_flags.empty_title') }}</div>
        <div class="muted" style="max-width: 520px; margin: 0 auto 12px">
          {{ $t('page_admin_feature_flags.empty_hint') }}
        </div>
        <a-card size="small" style="max-width: 520px; margin: 0 auto; text-align: left">
          <ul style="margin: 0; padding-left: 18px">
            <li v-for="kk in knownKeys" :key="kk.key" style="margin-bottom: 6px">
              <code>{{ kk.key }}</code>
              <a-tag
                :color="kk.type === 'bool' ? 'green' : 'geekblue'"
                style="margin-left: 6px"
              >
                {{ kk.type }}
              </a-tag>
              <div class="muted" style="font-size: 11px">{{ kk.hint }}</div>
            </li>
          </ul>
        </a-card>
      </div>
    </a-card>
  </div>
</template>
