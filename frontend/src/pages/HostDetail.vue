<script setup lang="ts">
import { defineAsyncComponent, onMounted, onUnmounted, ref, watch, computed } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import dayjs from 'dayjs';
import { ArrowLeftOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons-vue';
import { getResources, listBatches, getBatchEta, deleteHost } from '../api/client';
import { useAuthStore } from '../store/auth';
import { usePermissions } from '../composables/usePermissions';
import type { GetHostTimeseriesParams } from '../api/client';
import type { ResourceSnapshot, Batch } from '../types';
import { fmtRelative, fmtDuration } from '../utils/format';

// Both charts pull in echarts. Lazy-load so the latest-snapshot summary
// statistics render instantly — the charts can populate a moment later.
const ResourceChart = defineAsyncComponent(() => import('../components/ResourceChart.vue'));
const StackedResourceChart = defineAsyncComponent(
  () => import('../components/StackedResourceChart.vue'),
);

const { t } = useI18n();
const props = defineProps<{ host: string }>();
const router = useRouter();
const auth = useAuthStore();
const { canWrite } = usePermissions();
const isAdmin = computed(() => auth.isAdmin);

const deleting = ref(false);
async function handleDeleteHost() {
  if (deleting.value) return;
  deleting.value = true;
  try {
    await deleteHost(props.host);
    router.push('/hosts');
  } catch {
    // interceptor notifies
  } finally {
    deleting.value = false;
  }
}

const snapshots = ref<ResourceSnapshot[]>([]);
const recentBatches = ref<Batch[]>([]);
/** eta_seconds per batch_id for running batches. */
const batchEtaMap = ref<Record<string, number | null>>({});
const loading = ref(false);
const rangeHours = ref<number>(6);
const autoRefresh = ref(true);
let timer: number | null = null;

async function fetchBatchEtas(batches: Batch[]) {
  const running = batches.filter((b) => b.status === 'running');
  if (!running.length) return;
  const results = await Promise.allSettled(
    running.map((b) => getBatchEta(b.id).then((r) => ({ id: b.id, eta: r.eta_seconds }))),
  );
  const map: Record<string, number | null> = {};
  for (const r of results) {
    if (r.status === 'fulfilled') map[r.value.id] = r.value.eta;
  }
  batchEtaMap.value = map;
}

function fmtBatchEta(batchId: string): string {
  if (!(batchId in batchEtaMap.value)) return '';
  const s = batchEtaMap.value[batchId];
  if (s === null) return '';
  return `~${fmtDuration(s)}`;
}

// Stacked chart section
const stackedMetric = ref<GetHostTimeseriesParams['metric']>('gpu_mem_mb');
const stackedWindowHours = ref<number>(2);

const metricOptions: Array<{ label: string; value: GetHostTimeseriesParams['metric'] }> = [
  { label: 'GPU mem (MB)', value: 'gpu_mem_mb' },
  { label: 'GPU util (%)', value: 'gpu_util_pct' },
  { label: 'CPU util (%)', value: 'cpu_util_pct' },
  { label: 'RAM (MB)', value: 'ram_mb' },
];

/** Latest snapshot for the summary block. */
const latestSnap = computed<ResourceSnapshot | null>(() => {
  if (!snapshots.value.length) return null;
  return [...snapshots.value].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))[0];
});

async function fetchAll() {
  loading.value = true;
  try {
    const since = dayjs().subtract(rangeHours.value, 'hour').toISOString();
    const [snaps, batches] = await Promise.all([
      getResources({ host: props.host, since, limit: 2000 }),
      listBatches({ limit: 20 }).catch((): Batch[] => []),
    ]);
    snapshots.value = snaps;
    recentBatches.value = batches.filter((b) => b.host === props.host);
    fetchBatchEtas(recentBatches.value);
  } finally {
    loading.value = false;
  }
}

function startTimer() {
  stopTimer();
  if (autoRefresh.value) timer = window.setInterval(fetchAll, 15_000);
}
function stopTimer() {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

watch([autoRefresh, rangeHours], () => {
  fetchAll();
  startTimer();
});

onMounted(() => {
  fetchAll();
  startTimer();
});
onUnmounted(stopTimer);

// For range picker: last N hours shortcuts
const presets: Array<[string, number]> = [
  ['1h', 1],
  ['6h', 6],
  ['24h', 24],
  ['7d', 24 * 7],
];

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
</script>

<template>
  <div class="page-container">
    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px">
      <a-button size="small" @click="router.push('/hosts')">
        <template #icon><ArrowLeftOutlined /></template>
        {{ $t('page_host_detail.back') }}
      </a-button>
      <div style="font-size: 16px; font-weight: 500">{{ host }}</div>
      <a-button size="small" :loading="loading" @click="fetchAll">
        <template #icon><ReloadOutlined /></template>
      </a-button>

      <span style="flex: 1" />

      <a-radio-group v-model:value="rangeHours" size="small">
        <a-radio-button v-for="[lbl, h] in presets" :key="lbl" :value="h">{{ lbl }}</a-radio-button>
      </a-radio-group>
      <a-switch
        v-model:checked="autoRefresh"
        :checked-children="$t('page_dashboard.auto_on')"
        :un-checked-children="$t('page_dashboard.auto_off')"
      />

      <a-popconfirm
        v-if="canWrite && isAdmin"
        :title="$t('common.confirm_delete_host')"
        :ok-text="$t('common.delete')"
        :cancel-text="$t('common.cancel')"
        ok-type="danger"
        @confirm="handleDeleteHost"
      >
        <a-button size="small" danger :loading="deleting">
          <template #icon><DeleteOutlined /></template>
          {{ $t('common.delete') }}
        </a-button>
      </a-popconfirm>
    </div>

    <!-- Latest snapshot summary block -->
    <a-card
      v-if="latestSnap"
      size="small"
      :title="$t('page_host_detail.header_latest_snapshot')"
      style="margin-bottom: 16px"
    >
      <div style="font-size: 11px; color: var(--text-tertiary); margin-bottom: 8px">
        {{ fmtRelative(latestSnap.timestamp) }}
      </div>
      <a-row :gutter="[12, 8]">
        <a-col :xs="12" :sm="8" :md="4">
          <a-statistic
            :title="$t('page_host_detail.chart_cpu_util')"
            :value="latestSnap.cpu_util_pct != null ? Math.round(latestSnap.cpu_util_pct) : '—'"
            :suffix="latestSnap.cpu_util_pct != null ? '%' : ''"
          />
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <a-statistic
            :title="$t('page_host_detail.stat_ram')"
            :value="gbStr(latestSnap.ram_mb, latestSnap.ram_total_mb)"
          />
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <a-statistic
            :title="$t('page_host_detail.stat_disk')"
            :value="diskGbStr(latestSnap.disk_free_mb)"
          />
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <a-statistic
            :title="$t('page_host_detail.chart_gpu_util')"
            :value="latestSnap.gpu_util_pct != null ? Math.round(latestSnap.gpu_util_pct) : '—'"
            :suffix="latestSnap.gpu_util_pct != null ? '%' : ''"
          />
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <a-statistic
            :title="$t('page_host_detail.stat_vram')"
            :value="gbStr(latestSnap.gpu_mem_mb, latestSnap.gpu_mem_total_mb)"
          />
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <a-statistic
            :title="$t('page_host_detail.stat_temp')"
            :value="latestSnap.gpu_temp_c != null ? Math.round(latestSnap.gpu_temp_c) : '—'"
            :suffix="latestSnap.gpu_temp_c != null ? '°C' : ''"
          />
        </a-col>
      </a-row>
    </a-card>

    <!-- Resource timeseries chart -->
    <a-card size="small" style="margin-bottom: 16px">
      <ResourceChart v-if="snapshots.length" :snapshots="snapshots" />
      <div v-else class="muted empty-wrap">
        {{ $t('page_host_detail.empty', { host }) }}
      </div>
    </a-card>

    <!-- Stacked by-batch breakdown -->
    <a-card
      size="small"
      :title="$t('page_host_detail.stacked_title')"
      style="margin-bottom: 16px"
    >
      <template #extra>
        <a-select
          v-model:value="stackedMetric"
          size="small"
          style="width: 160px"
          :placeholder="$t('page_host_detail.metric_select_label')"
        >
          <a-select-option
            v-for="opt in metricOptions"
            :key="opt.value"
            :value="opt.value"
          >
            {{ opt.label }}
          </a-select-option>
        </a-select>
      </template>
      <StackedResourceChart
        :host="props.host"
        :metric="stackedMetric"
        :window-hours="stackedWindowHours"
        :height="260"
      />
    </a-card>

    <!-- Recent batches on this host -->
    <a-card v-if="recentBatches.length" size="small" :title="$t('page_host_detail.header_recent_batches')">
      <a-list :data-source="recentBatches" size="small">
        <template #renderItem="{ item }">
          <a-list-item>
            <a
              href="#"
              @click.prevent="router.push(`/batches/${encodeURIComponent((item as Batch).id)}`)"
            >{{ (item as Batch).id }}</a>
            <span class="muted" style="font-size: 12px; margin-left: 8px">
              {{ (item as Batch).project }} · {{ (item as Batch).status }}
            </span>
            <span
              v-if="(item as Batch).status === 'running' && fmtBatchEta((item as Batch).id)"
              style="font-size: 12px; margin-left: 8px; color: #1890ff"
            >
              ⏱ {{ $t('component_eta.eta_label') }}: {{ fmtBatchEta((item as Batch).id) }}
            </span>
          </a-list-item>
        </template>
      </a-list>
    </a-card>
  </div>
</template>
