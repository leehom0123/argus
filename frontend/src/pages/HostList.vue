<script setup lang="ts">
import { onMounted, ref, computed } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import {
  ReloadOutlined,
  AppstoreOutlined,
  UnorderedListOutlined,
  DeleteOutlined,
} from '@ant-design/icons-vue';
import { message } from 'ant-design-vue';
import { listHosts, getResources, listBatches, bulkDeleteHosts } from '../api/client';
import { useAuthStore } from '../store/auth';
import type { ResourceSnapshot, Batch, HostSummary } from '../types';
import { fmtRelative } from '../utils/format';
import { cached, invalidate, cacheKey, cacheTtl } from '../composables/useCache';
import { statusBorderColor, hostAggregateStatus } from '../utils/status';
import EmptyState from '../components/EmptyState.vue';
import HostCard from '../components/HostCard.vue';

const { t } = useI18n();
const router = useRouter();
const auth = useAuthStore();

const selectedHosts = ref<string[]>([]);
const bulkDeleting = ref(false);

const rowSelection = computed(() => ({
  selectedRowKeys: selectedHosts.value,
  onChange: (keys: (string | number)[]) => {
    selectedHosts.value = keys.map(String);
  },
}));

async function runBulkDelete() {
  if (!selectedHosts.value.length || bulkDeleting.value) return;
  bulkDeleting.value = true;
  try {
    const ids = selectedHosts.value.slice();
    const res = await bulkDeleteHosts(ids);
    if (res.skipped.length === 0) {
      message.success(t('common.bulk_delete_success', { n: res.deleted.length }));
    } else {
      message.warning(
        t('common.bulk_delete_partial', {
          deleted: res.deleted.length,
          total: ids.length,
        }),
      );
    }
    selectedHosts.value = [];
    void fetchAll(true);
  } catch {
    // interceptor notifies
  } finally {
    bulkDeleting.value = false;
  }
}

/**
 * HostRow packs everything the grid + table views need so both modes
 * share one data source. The HostCard consumes the ``summary`` shape
 * (parallels the /api/dashboard hosts payload); the table view reads
 * ``snap``/``activeBatchCount`` directly.
 */
interface HostRow {
  host: string;
  snap: ResourceSnapshot | null;
  batches: Batch[];
  activeBatchCount: number;
  activeJobCount: number;
  pids: number[];
  summary: HostSummary;
}

const rows = ref<HostRow[]>([]);
const loading = ref(false);

/**
 * Persist the view mode so a user's preference survives a refresh. Grid
 * is the default (matches ProjectList.vue on first paint).
 */
const VIEW_MODE_KEY = 'argus.host-list.view-mode';
const initialViewMode =
  (localStorage.getItem(VIEW_MODE_KEY) as 'grid' | 'table' | null) ?? 'grid';
const viewMode = ref<'grid' | 'table'>(initialViewMode);
function setViewMode(m: 'grid' | 'table') {
  viewMode.value = m;
  localStorage.setItem(VIEW_MODE_KEY, m);
}
function onViewModeChange(e: unknown): void {
  const v = (e as { target?: { value?: unknown } })?.target?.value;
  if (v === 'grid' || v === 'table') setViewMode(v);
}

function gbStr(mb?: number | null, totalMb?: number | null): string {
  if (mb == null) return '—';
  const used = (mb / 1024).toFixed(1);
  if (totalMb != null) return `${used} / ${(totalMb / 1024).toFixed(1)} GB`;
  return `${used} GB`;
}

function diskGbStr(mb?: number | null): string {
  if (mb == null) return '—';
  return `${(mb / 1024).toFixed(1)} GB`;
}

function utilColor(pct?: number | null): string {
  if (pct == null) return 'default';
  if (pct < 60) return 'green';
  if (pct < 80) return 'gold';
  if (pct < 90) return 'orange';
  return 'red';
}

function tempColor(c?: number | null): string {
  if (c == null) return 'default';
  if (c < 70) return 'green';
  if (c < 80) return 'gold';
  return 'red';
}

function ratioColor(used?: number | null, total?: number | null): string {
  if (used == null || total == null || total === 0) return 'default';
  return utilColor(Math.round((used / total) * 100));
}

/**
 * Convert a HostRow into the HostSummary shape the HostCard consumes.
 * Keeps the table mode untouched while letting grid mode lean on the
 * existing card component without a separate fetch.
 */
function toHostSummary(r: HostRow): HostSummary {
  return {
    host: r.host,
    gpu_util_pct: r.snap?.gpu_util_pct ?? null,
    gpu_mem_mb: r.snap?.gpu_mem_mb ?? null,
    gpu_mem_total_mb: r.snap?.gpu_mem_total_mb ?? null,
    gpu_temp_c: r.snap?.gpu_temp_c ?? null,
    cpu_util_pct: r.snap?.cpu_util_pct ?? null,
    ram_mb: r.snap?.ram_mb ?? null,
    ram_total_mb: r.snap?.ram_total_mb ?? null,
    disk_free_mb: r.snap?.disk_free_mb ?? null,
    running_jobs: r.activeJobCount,
    last_seen: r.snap?.timestamp ?? null,
  };
}

async function fetchAll(force = false) {
  loading.value = true;
  try {
    if (force) invalidate(cacheKey.hosts());
    // Host names change rarely — cache them. Running batches always fresh.
    // Issue both in parallel so the page isn't waiting on them back-to-back.
    const [names, runningBatches] = await Promise.all([
      cached(cacheKey.hosts(), listHosts, cacheTtl.hosts),
      listBatches({ status: 'running', limit: 200 }).catch((): Batch[] => []),
    ]);
    const hosts = names ?? [];

    const batchesByHost = new Map<string, Batch[]>();
    for (const b of runningBatches) {
      if (b.host) {
        if (!batchesByHost.has(b.host)) batchesByHost.set(b.host, []);
        batchesByHost.get(b.host)!.push(b);
      }
    }

    const results = await Promise.all(
      hosts.map(async (h) => {
        let snap: ResourceSnapshot | null = null;
        try {
          const snaps = await getResources({ host: h, limit: 1 });
          snap = snaps?.[0] ?? null;
        } catch { /* ignore */ }

        const hostBatches = batchesByHost.get(h) ?? [];
        const activeJobCount = hostBatches.reduce(
          (sum, b) => sum + (b.n_done < b.n_total ? b.n_total - b.n_done : 0),
          0,
        );

        // Collect PIDs from extra fields (if any snapshot has them).
        const pids: number[] = [];
        if (snap && typeof (snap as Record<string, unknown>).pid === 'number') {
          pids.push((snap as Record<string, unknown>).pid as number);
        }

        const row: HostRow = {
          host: h,
          snap,
          batches: hostBatches,
          activeBatchCount: hostBatches.length,
          activeJobCount,
          pids,
          summary: {} as HostSummary, // placeholder, filled below
        };
        // Attach batches onto the HostSummary so hostAggregateStatus
        // can pick the worst batch status when rendering the border.
        const summary = toHostSummary(row) as HostSummary & {
          batches?: Batch[];
        };
        summary.batches = hostBatches;
        row.summary = summary;
        return row;
      }),
    );

    rows.value = results;
  } finally {
    loading.value = false;
  }
}

function open(h: string) {
  router.push(`/hosts/${encodeURIComponent(h)}`);
}

const columns = computed(() => [
  { title: t('page_host_list.col_host'), key: 'host', dataIndex: 'host', width: 160, fixed: 'left' as const },
  { title: t('page_host_list.col_last_seen'), key: 'last_seen', width: 160 },
  { title: t('page_host_list.col_cpu'), key: 'cpu', width: 100 },
  { title: t('page_host_list.col_ram'), key: 'ram', width: 160 },
  { title: t('page_host_list.col_disk'), key: 'disk', width: 120 },
  { title: t('page_host_list.col_gpu'), key: 'gpu', width: 100 },
  { title: t('page_host_list.col_vram'), key: 'vram', width: 160 },
  { title: t('page_host_list.col_temp'), key: 'temp', width: 90 },
  { title: t('page_host_list.col_status'), key: 'status', width: 110 },
  { title: t('page_host_list.col_active_batches'), key: 'active_batches', width: 110 },
  { title: t('page_host_list.col_active_jobs'), key: 'active_jobs', width: 110 },
  { title: t('page_host_list.col_pids'), key: 'pids', width: 120 },
]);

/**
 * Ant's ``customRow`` lets us inject a per-row style. We wrap the first
 * cell with a left-border of the host's aggregate status, matching the
 * grid-mode HostCard. Rows for idle hosts stay transparent.
 */
function rowAttrs(record: HostRow) {
  return {
    onClick: () => open(record.host),
    style: {
      cursor: 'pointer',
      boxShadow: `inset 4px 0 0 0 ${statusBorderColor(hostAggregateStatus(record.summary))}`,
    },
  };
}

onMounted(() => fetchAll());
</script>

<template>
  <div class="page-container">
    <div
      style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap"
    >
      <div style="font-size: 16px; font-weight: 500">
        {{ $t('page_host_list.title') }}
      </div>
      <span style="flex: 1" />
      <!-- Grid/table toggle — mirrors ProjectList.vue for consistency. -->
      <a-radio-group
        :value="viewMode"
        button-style="solid"
        size="small"
        @change="onViewModeChange"
      >
        <a-radio-button value="grid">
          <AppstoreOutlined />
        </a-radio-button>
        <a-radio-button value="table">
          <UnorderedListOutlined />
        </a-radio-button>
      </a-radio-group>
      <a-button size="small" :loading="loading" @click="fetchAll(true)">
        <template #icon><ReloadOutlined /></template>
      </a-button>

      <a-popconfirm
        v-if="auth.isAdmin && selectedHosts.length > 0"
        :title="$t('common.bulk_delete_confirm', { n: selectedHosts.length })"
        :ok-text="$t('common.delete')"
        :cancel-text="$t('common.cancel')"
        ok-type="danger"
        @confirm="runBulkDelete"
      >
        <a-button size="small" danger :loading="bulkDeleting">
          <template #icon><DeleteOutlined /></template>
          {{ $t('common.bulk_delete_button', { n: selectedHosts.length }) }}
        </a-button>
      </a-popconfirm>
    </div>

    <EmptyState
      v-if="!rows.length && !loading"
      variant="empty_hosts"
      :title="$t('page_host_list.empty')"
    />

    <!-- Grid view — responsive 1/2/3-column with HostCard per host. -->
    <a-row v-else-if="viewMode === 'grid'" :gutter="[16, 16]">
      <a-col
        v-for="r in rows"
        :key="r.host"
        :xs="24"
        :sm="12"
        :xl="8"
      >
        <HostCard :host="r.summary" />
      </a-col>
    </a-row>

    <!-- Table view — dense multi-column layout with status-colored rows. -->
    <a-table
      v-else
      :columns="columns"
      :data-source="rows"
      row-key="host"
      size="small"
      :loading="loading"
      :scroll="{ x: 1300 }"
      :pagination="false"
      :row-selection="auth.isAdmin ? rowSelection : undefined"
      :custom-row="rowAttrs"
      :row-class-name="() => 'host-row-clickable'"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'host'">
          <a href="#" @click.prevent="open((record as HostRow).host)">
            {{ (record as HostRow).host }}
          </a>
        </template>

        <template v-else-if="column.key === 'last_seen'">
          <span v-if="(record as HostRow).snap" class="muted" style="font-size: 12px">
            {{ fmtRelative((record as HostRow).snap!.timestamp) }}
          </span>
          <span v-else class="muted">—</span>
        </template>

        <template v-else-if="column.key === 'cpu'">
          <a-tag
            v-if="(record as HostRow).snap?.cpu_util_pct != null"
            :color="utilColor((record as HostRow).snap!.cpu_util_pct)"
            style="font-size: 11px; padding: 0 5px"
          >
            {{ Math.round((record as HostRow).snap!.cpu_util_pct!) }}%
          </a-tag>
          <span v-else class="muted">—</span>
        </template>

        <template v-else-if="column.key === 'ram'">
          <a-tag
            v-if="(record as HostRow).snap?.ram_mb != null"
            :color="ratioColor((record as HostRow).snap!.ram_mb, (record as HostRow).snap!.ram_total_mb)"
            style="font-size: 11px; padding: 0 5px"
          >
            {{ gbStr((record as HostRow).snap!.ram_mb, (record as HostRow).snap!.ram_total_mb) }}
          </a-tag>
          <span v-else class="muted">—</span>
        </template>

        <template v-else-if="column.key === 'disk'">
          <span v-if="(record as HostRow).snap?.disk_free_mb != null">
            {{ diskGbStr((record as HostRow).snap!.disk_free_mb) }}
          </span>
          <span v-else class="muted">—</span>
        </template>

        <template v-else-if="column.key === 'gpu'">
          <a-tag
            v-if="(record as HostRow).snap?.gpu_util_pct != null"
            :color="utilColor((record as HostRow).snap!.gpu_util_pct)"
            style="font-size: 11px; padding: 0 5px"
          >
            {{ Math.round((record as HostRow).snap!.gpu_util_pct!) }}%
          </a-tag>
          <span v-else class="muted">—</span>
        </template>

        <template v-else-if="column.key === 'vram'">
          <a-tag
            v-if="(record as HostRow).snap?.gpu_mem_mb != null"
            :color="ratioColor((record as HostRow).snap!.gpu_mem_mb, (record as HostRow).snap!.gpu_mem_total_mb)"
            style="font-size: 11px; padding: 0 5px"
          >
            {{ gbStr((record as HostRow).snap!.gpu_mem_mb, (record as HostRow).snap!.gpu_mem_total_mb) }}
          </a-tag>
          <span v-else class="muted">—</span>
        </template>

        <template v-else-if="column.key === 'temp'">
          <a-tag
            v-if="(record as HostRow).snap?.gpu_temp_c != null"
            :color="tempColor((record as HostRow).snap!.gpu_temp_c)"
            style="font-size: 11px; padding: 0 5px"
          >
            🌡️ {{ Math.round((record as HostRow).snap!.gpu_temp_c!) }}°C
          </a-tag>
          <span v-else class="muted">—</span>
        </template>

        <template v-else-if="column.key === 'status'">
          <span
            :style="{
              display: 'inline-block',
              padding: '0 6px',
              fontSize: '11px',
              borderLeft: `3px solid ${statusBorderColor(hostAggregateStatus((record as HostRow).summary))}`,
            }"
          >
            {{ hostAggregateStatus((record as HostRow).summary) || '—' }}
          </span>
        </template>

        <template v-else-if="column.key === 'active_batches'">
          <a-tag v-if="(record as HostRow).activeBatchCount > 0" color="blue">
            {{ (record as HostRow).activeBatchCount }}
          </a-tag>
          <span v-else class="muted">0</span>
        </template>

        <template v-else-if="column.key === 'active_jobs'">
          {{ (record as HostRow).activeJobCount || '—' }}
        </template>

        <template v-else-if="column.key === 'pids'">
          <span v-if="(record as HostRow).pids.length">
            {{ (record as HostRow).pids.join(', ') }}
          </span>
          <span v-else class="muted">—</span>
        </template>
      </template>
    </a-table>
  </div>
</template>

<style scoped>
:deep(.host-row-clickable) {
  cursor: pointer;
}
</style>
