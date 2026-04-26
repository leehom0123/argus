<script setup lang="ts">
/**
 * /admin/audit-log — server-side paginated list of audit events.
 */

import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import dayjs, { type Dayjs } from 'dayjs';
import { ReloadOutlined } from '@ant-design/icons-vue';
import { listAuditLog } from '../../api/admin';
import type { AuditLogEntry, ListAuditParams } from '../../types';
import { fmtTime, fmtRelative } from '../../utils/format';

type DateRange = [Dayjs, Dayjs] | undefined;

const { t } = useI18n();
const PAGE_SIZE = 50;

const entries = ref<AuditLogEntry[]>([]);
const loading = ref(false);
const offset = ref(0);
const reachedEnd = ref(false);

// ---- Filters ----
const actionFilter = ref<string | undefined>(undefined);
const dateRange = ref<[Dayjs | null, Dayjs | null] | null>(null);

// ---- Auto-refresh ----
const autoRefresh = ref(false);
let timer: number | null = null;

const ACTION_COLOR: Record<string, string> = {
  token_create: 'blue',
  token_revoke: 'orange',
  share_add: 'green',
  share_remove: 'orange',
  public_share_add: 'purple',
  public_share_revoke: 'orange',
  batch_delete: 'red',
  user_ban: 'red',
  user_unban: 'green',
  flag_update: 'geekblue',
  login: 'cyan',
  login_failed: 'orange',
  logout: 'default',
};

function colorFor(action: string): string {
  return ACTION_COLOR[action] ?? 'default';
}

const knownActions = Object.keys(ACTION_COLOR);

const columns = computed(() => [
  { title: t('page_admin_audit_log.col_time'), key: 'timestamp', width: 190, fixed: 'left' as const },
  { title: t('page_admin_audit_log.col_user'), key: 'user', width: 140 },
  { title: t('page_admin_audit_log.col_action'), key: 'action', width: 160 },
  { title: t('page_admin_audit_log.col_target'), key: 'target', width: 260 },
  { title: t('page_admin_audit_log.col_metadata'), key: 'metadata' },
  { title: t('page_admin_audit_log.col_ip'), key: 'ip_address', width: 140 },
]);

function buildParams(extra: Partial<ListAuditParams> = {}): ListAuditParams {
  const p: ListAuditParams = { limit: PAGE_SIZE, ...extra };
  if (actionFilter.value) p.action = actionFilter.value;
  const from = dateRange.value?.[0];
  if (from) p.since = from.toISOString();
  return p;
}

async function fetchFirstPage(): Promise<void> {
  loading.value = true;
  try {
    const rows = (await listAuditLog(buildParams({ offset: 0 }))) ?? [];
    entries.value = applyDateUpperBound(rows);
    offset.value = rows.length;
    reachedEnd.value = rows.length < PAGE_SIZE;
  } catch {
    // interceptor notifies
  } finally {
    loading.value = false;
  }
}

async function fetchNextPage(): Promise<void> {
  if (reachedEnd.value) return;
  loading.value = true;
  try {
    const rows = (await listAuditLog(buildParams({ offset: offset.value }))) ?? [];
    entries.value = entries.value.concat(applyDateUpperBound(rows));
    offset.value += rows.length;
    if (rows.length < PAGE_SIZE) reachedEnd.value = true;
  } catch {
    // interceptor notifies
  } finally {
    loading.value = false;
  }
}

function applyDateUpperBound(rows: AuditLogEntry[]): AuditLogEntry[] {
  const to = dateRange.value?.[1];
  if (!to) return rows;
  const cutoff = to.endOf('day').valueOf();
  return rows.filter((r) => {
    const ts = dayjs(r.timestamp).valueOf();
    return Number.isFinite(ts) ? ts <= cutoff : true;
  });
}

function applyFilters(): void {
  void fetchFirstPage();
}

function clearFilters(): void {
  actionFilter.value = undefined;
  dateRange.value = null;
  void fetchFirstPage();
}

function startTimer(): void {
  stopTimer();
  if (!autoRefresh.value) return;
  timer = window.setInterval(() => fetchFirstPage(), 30_000);
}

function stopTimer(): void {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

function onAutoRefreshToggle(v: boolean | string | number): void {
  autoRefresh.value = !!v;
  startTimer();
}

function metadataPreview(meta: unknown): string {
  if (meta == null) return '';
  if (typeof meta === 'string') {
    try {
      const parsed = JSON.parse(meta);
      return JSON.stringify(parsed);
    } catch {
      return meta;
    }
  }
  try {
    return JSON.stringify(meta);
  } catch {
    return String(meta);
  }
}

function metadataPretty(meta: unknown): string {
  if (meta == null) return '';
  let obj: unknown = meta;
  if (typeof meta === 'string') {
    try {
      obj = JSON.parse(meta);
    } catch {
      return meta;
    }
  }
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj);
  }
}

function targetLabel(r: AuditLogEntry): string {
  if (!r.target_type && !r.target_id) return '';
  if (r.target_type && r.target_id) return `${r.target_type}:${r.target_id}`;
  return r.target_type ?? r.target_id ?? '';
}

const hasActiveFilter = computed(
  () => actionFilter.value !== undefined || dateRange.value !== null,
);

onMounted(() => {
  void fetchFirstPage();
});

onUnmounted(stopTimer);
</script>

<template>
  <div class="page-container" style="max-width: 1400px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_admin_audit_log.breadcrumb_admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_admin_audit_log.breadcrumb_audit') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="$t('page_admin_audit_log.card_title')">
      <template #extra>
        <a-space>
          <a-tooltip :title="$t('page_admin_audit_log.tooltip_auto_refresh')">
            <a-switch
              :checked="autoRefresh"
              :checked-children="$t('page_dashboard.auto_on')"
              :un-checked-children="$t('page_dashboard.auto_off')"
              @change="onAutoRefreshToggle"
            />
          </a-tooltip>
          <a-button :loading="loading" @click="fetchFirstPage">
            <template #icon><ReloadOutlined /></template>
            {{ $t('page_admin_audit_log.btn_refresh') }}
          </a-button>
        </a-space>
      </template>

      <div class="filter-bar">
        <a-select
          v-model:value="actionFilter"
          :placeholder="$t('page_admin_audit_log.filter_action_placeholder')"
          allow-clear
          style="width: 220px"
          :options="knownActions.map((a) => ({ value: a, label: a }))"
          @change="applyFilters"
        />
        <a-range-picker v-model:value="dateRange as any" :show-time="false" @change="applyFilters" />

        <a-button v-if="hasActiveFilter" size="small" @click="clearFilters">
          {{ $t('page_admin_audit_log.btn_clear_filters') }}
        </a-button>

        <span style="flex: 1" />

        <span class="muted" style="font-size: 12px">
          {{ reachedEnd
            ? $t('page_admin_audit_log.loaded_end', { count: entries.length })
            : $t('page_admin_audit_log.loaded_count', { count: entries.length })
          }}
        </span>
      </div>

      <a-table
        :columns="columns"
        :data-source="entries"
        :loading="loading"
        row-key="id"
        size="small"
        :scroll="{ x: 1200 }"
        :pagination="false"
        :expand-row-by-click="false"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'timestamp'">
            <div style="line-height: 1.2">
              <div>{{ fmtTime((record as AuditLogEntry).timestamp) }}</div>
              <div class="muted" style="font-size: 11px">
                {{ fmtRelative((record as AuditLogEntry).timestamp) }}
              </div>
            </div>
          </template>

          <template v-else-if="column.key === 'user'">
            <span v-if="(record as AuditLogEntry).username">
              {{ (record as AuditLogEntry).username }}
            </span>
            <span v-else-if="(record as AuditLogEntry).user_id != null" class="muted">
              #{{ (record as AuditLogEntry).user_id }}
            </span>
            <span v-else class="muted">{{ $t('page_admin_audit_log.anonymous') }}</span>
          </template>

          <template v-else-if="column.key === 'action'">
            <a-tag :color="colorFor((record as AuditLogEntry).action)">
              {{ (record as AuditLogEntry).action }}
            </a-tag>
          </template>

          <template v-else-if="column.key === 'target'">
            <code v-if="targetLabel(record as AuditLogEntry)" style="font-size: 11px">
              {{ targetLabel(record as AuditLogEntry) }}
            </code>
            <span v-else class="muted">—</span>
          </template>

          <template v-else-if="column.key === 'metadata'">
            <template v-if="(record as AuditLogEntry).metadata == null">
              <span class="muted">—</span>
            </template>
            <a-tooltip
              v-else
              placement="topLeft"
              overlay-class-name="audit-meta-tip"
            >
              <template #title>
                <pre style="margin: 0; font-size: 11px; max-width: 520px; white-space: pre-wrap">{{
                  metadataPretty((record as AuditLogEntry).metadata)
                }}</pre>
              </template>
              <code
                style="
                  font-size: 11px;
                  display: inline-block;
                  max-width: 520px;
                  overflow: hidden;
                  text-overflow: ellipsis;
                  white-space: nowrap;
                  vertical-align: middle;
                "
              >
                {{ metadataPreview((record as AuditLogEntry).metadata) }}
              </code>
            </a-tooltip>
          </template>

          <template v-else-if="column.key === 'ip_address'">
            <code v-if="(record as AuditLogEntry).ip_address" style="font-size: 11px">
              {{ (record as AuditLogEntry).ip_address }}
            </code>
            <span v-else class="muted">—</span>
          </template>
        </template>

        <template #emptyText>
          <div v-if="!loading" class="empty-wrap">
            <div style="font-size: 15px; margin-bottom: 6px">
              <template v-if="hasActiveFilter">{{ $t('page_admin_audit_log.empty_filtered') }}</template>
              <template v-else>{{ $t('page_admin_audit_log.empty_default') }}</template>
            </div>
            <div class="muted" style="max-width: 520px; margin: 0 auto">
              {{ $t('page_admin_audit_log.empty_hint') }}
            </div>
          </div>
        </template>
      </a-table>

      <div style="display: flex; justify-content: center; margin-top: 14px">
        <a-button
          v-if="!reachedEnd && entries.length > 0"
          :loading="loading"
          @click="fetchNextPage"
        >
          {{ $t('page_admin_audit_log.load_more', { count: PAGE_SIZE }) }}
        </a-button>
        <span v-else-if="entries.length > 0" class="muted" style="font-size: 12px">
          {{ $t('page_admin_audit_log.end_of_log') }}
        </span>
      </div>
    </a-card>
  </div>
</template>
