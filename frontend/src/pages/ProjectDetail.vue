<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  ReloadOutlined,
  ShareAltOutlined,
  LinkOutlined,
  CopyOutlined,
} from '@ant-design/icons-vue';
import { listBatches, deleteProject } from '../api/client';
import { useAuthStore } from '../store/auth';
import {
  getProject,
  getProjectActiveBatches,
  getProjectLeaderboard,
  getProjectMatrix,
  getProjectResources,
} from '../api/projects';
import { listProjectShares } from '../api/shares';
import { exportProjectCsv, exportProjectRawCsv } from '../api/exports';
import { cached, peek, invalidate, cacheKey, cacheTtl } from '../composables/useCache';
import type {
  ActiveBatchCard,
  Batch,
  LeaderboardRow,
  MatrixData,
  ProjectDetail,
  ProjectResourcesData,
  ProjectShare,
} from '../types';
import BatchCard from '../components/BatchCard.vue';
import StatusTag from '../components/StatusTag.vue';
import ProgressInline from '../components/ProgressInline.vue';
import StarButton from '../components/StarButton.vue';
import ExportCsvButton from '../components/ExportCsvButton.vue';
import ShareDialog from '../components/ShareDialog.vue';
import AnonymousCTA from '../components/AnonymousCTA.vue';
import ProjectRecipientsPanel from '../components/ProjectRecipientsPanel.vue';
import { usePermissions } from '../composables/usePermissions';
import { useChart } from '../composables/useChart';
import { fmtRelative, fmtDuration } from '../utils/format';

const props = defineProps<{
  project: string;
  /** When true mount in read-only mode (hides share / collaborator actions). */
  readOnly?: boolean;
}>();
const router = useRouter();
const { t } = useI18n();
const { canWrite, isAnonymous } = usePermissions(props.readOnly);
const auth = useAuthStore();
const isAdmin = computed(() => auth.isAdmin);

const deleting = ref(false);
async function handleDeleteProject() {
  if (deleting.value) return;
  deleting.value = true;
  try {
    await deleteProject(props.project);
    router.push('/projects');
  } catch {
    // interceptor notifies
  } finally {
    deleting.value = false;
  }
}

// ---- data refs ----
// Stale-while-revalidate: if the user hovered the project card on the
// previous page, we've already got a cached summary — paint it now so the
// header doesn't flash empty while the network call settles.
const detail = ref<ProjectDetail | null>(peek<ProjectDetail>(cacheKey.projectSummary(props.project)));
const active = ref<ActiveBatchCard[]>([]);
const recent = ref<Batch[]>([]);
const leaderboard = ref<LeaderboardRow[]>([]);
const matrix = ref<MatrixData | null>(null);
const resources = ref<ProjectResourcesData | null>(null);
const collaborators = ref<ProjectShare[]>([]);
const loading = ref(detail.value === null);
const matrixMetric = ref<string>('MSE');

// Which batch is currently selected for share dialog (null = closed).
const shareBatchId = ref<string | null>(null);
const shareOpen = computed({
  get: () => shareBatchId.value !== null,
  set: (v) => {
    if (!v) shareBatchId.value = null;
  },
});

const activeTab = ref<
  | 'active'
  | 'recent'
  | 'leaderboard'
  | 'matrix'
  | 'resources'
  | 'collaborators'
  | 'notifications'
>('active');

let autoRefreshTimer: number | null = null;

// ---- fetch helpers ----

async function fetchHeader() {
  try {
    // Use the cache so ProjectList → ProjectDetail reuses any prefetched
    // summary instead of hitting the network twice in quick succession.
    detail.value = await cached(
      cacheKey.projectSummary(props.project),
      () => getProject(props.project),
      cacheTtl.summary,
    );
  } catch {
    // interceptor already notified; fall through with empty state
  }
}

async function fetchActive() {
  try {
    active.value = (await getProjectActiveBatches(props.project)) ?? [];
  } catch {
    active.value = [];
  }
}

async function fetchRecent() {
  try {
    // Reuse the generic batches endpoint — filter to this project, newest first.
    recent.value = (await listBatches({ project: props.project, limit: 100 })) ?? [];
  } catch {
    recent.value = [];
  }
}

async function fetchLeaderboard() {
  try {
    // No metric param needed — backend returns all metrics; MSE is still the
    // default sort key on the server side so best rows bubble up first.
    leaderboard.value = (await getProjectLeaderboard(props.project)) ?? [];
  } catch {
    leaderboard.value = [];
  }
}

async function fetchMatrix() {
  try {
    matrix.value = await getProjectMatrix(props.project, matrixMetric.value);
  } catch {
    matrix.value = null;
  }
}

async function fetchResources() {
  try {
    resources.value = await getProjectResources(props.project);
  } catch {
    resources.value = null;
  }
}

async function fetchCollaborators() {
  try {
    const all = (await listProjectShares()) ?? [];
    collaborators.value = all.filter((s) => s.project === props.project);
  } catch {
    collaborators.value = [];
  }
}

async function refreshAll(force = false) {
  loading.value = detail.value === null;
  if (force) invalidate(cacheKey.projectSummary(props.project));
  await Promise.all([fetchHeader(), fetchActive(), fetchRecent()]);
  loading.value = false;
}

function onTabChange(key: string | number) {
  const k = String(key) as typeof activeTab.value;
  activeTab.value = k;
  // Lazy-load per tab.
  if (k === 'leaderboard' && !leaderboard.value.length) void fetchLeaderboard();
  if (k === 'matrix' && !matrix.value) void fetchMatrix();
  if (k === 'resources' && !resources.value) void fetchResources();
  if (k === 'collaborators') void fetchCollaborators();
}

watch(matrixMetric, () => void fetchMatrix());

// Auto-refresh only when the Active tab is visible and there is something running.
function applyAutoRefresh() {
  if (autoRefreshTimer !== null) {
    window.clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
  if (activeTab.value === 'active' && active.value.some((b) => b.status === 'running')) {
    autoRefreshTimer = window.setInterval(() => void fetchActive(), 10_000);
  }
}

watch([activeTab, active], applyAutoRefresh, { deep: true });

// ---- matrix chart (ECharts heatmap) ----

const matrixChartEl = ref<HTMLElement | null>(null);

const matrixOption = computed(() => {
  const m = matrix.value;
  if (!m || !m.models.length || !m.datasets.length) return null;
  const values = m.cells.map((c) => c.value).filter((v): v is number => v !== null && v !== undefined && Number.isFinite(v));
  const vmin = values.length ? Math.min(...values) : 0;
  const vmax = values.length ? Math.max(...values) : 1;

  // Build a lookup map from (datasetIdx, modelIdx) → batchIds for the tooltip.
  // m.batchIds is parallel to m.cells in the same row-major order.
  const batchIdMap = new Map<string, string[]>();
  if (m.batchIds) {
    m.cells.forEach((c, i) => {
      const bids = m.batchIds![i];
      if (bids && bids.length) {
        const key = `${m.datasets.indexOf(c.dataset)}_${m.models.indexOf(c.model)}`;
        batchIdMap.set(key, bids);
      }
    });
  }

  const heatmapData = m.cells.map((c) => [
    m.datasets.indexOf(c.dataset),
    m.models.indexOf(c.model),
    c.value ?? '-',
  ]);

  return {
    backgroundColor: 'transparent',
    tooltip: {
      position: 'top',
      formatter: (p: { data: [number, number, number | string] }) => {
        const [x, y, v] = p.data;
        const bids = batchIdMap.get(`${x}_${y}`);
        let batchLine = '';
        if (bids && bids.length) {
          const listed = bids.map((id) => id).join('<br/>· ');
          batchLine = `<br/>batch: · ${listed}`;
          if (bids.length === 3) batchLine += '<br/>(showing newest 3)';
        }
        return `${m.models[y]} · ${m.datasets[x]}<br/>${m.metric} = ${typeof v === 'number' ? v.toFixed(4) : '—'}${batchLine}`;
      },
    },
    grid: { left: 120, right: 60, top: 40, bottom: 80 },
    xAxis: {
      type: 'category',
      data: m.datasets,
      splitArea: { show: true },
      axisLabel: { rotate: 30 },
    },
    yAxis: { type: 'category', data: m.models, splitArea: { show: true } },
    visualMap: {
      min: vmin,
      max: vmax,
      calculable: true,
      orient: 'horizontal',
      left: 'center',
      bottom: 10,
      inRange: { color: ['#006edd', '#4096ff', '#ffe58f', '#ff7875', '#cf1322'] },
    },
    series: [
      {
        name: m.metric,
        type: 'heatmap',
        data: heatmapData,
        label: {
          show: true,
          formatter: (p: { data: [number, number, number | string] }) => {
            const [x, y, v] = p.data;
            const numStr = typeof v === 'number' ? (v as number).toFixed(3) : '—';
            // Show first 12 chars of the first batch id as a sub-label.
            // The \n is honoured by ECharts rich-text when `rich` is not set,
            // but we use a plain newline inside the formatter string; ECharts
            // splits on \n automatically for label text.
            const bids = batchIdMap.get(`${x}_${y}`);
            const subLabel = bids && bids.length ? bids[0].slice(0, 12) : '';
            return subLabel ? `${numStr}\n${subLabel}…` : numStr;
          },
          fontSize: 11,
          // Sub-label font shrinks to 9px via rich text is not needed here;
          // ECharts renders both lines at the specified fontSize.
        },
        emphasis: { itemStyle: { borderColor: '#fff', borderWidth: 1 } },
      },
    ],
  };
});

useChart(matrixChartEl, matrixOption);

// Register heatmap chart in the shared echarts core once (useChart's registry
// only pulls Line + Bar). We import + register directly here.
import * as echarts from 'echarts/core';
import { HeatmapChart } from 'echarts/charts';
import { VisualMapComponent, CalendarComponent } from 'echarts/components';
echarts.use([HeatmapChart, VisualMapComponent, CalendarComponent]);

// ---- resources chart (GPU-hours over time) ----

const resourcesChartEl = ref<HTMLElement | null>(null);

const resourcesOption = computed(() => {
  const r = resources.value;
  if (!r || !r.timeseries || r.timeseries.length === 0) return null;
  const xs = r.timeseries.map((p) => p.timestamp);
  const ghs = r.timeseries.map((p) => p.gpu_hours ?? null);
  const jobs = r.timeseries.map((p) => p.jobs_running ?? null);
  return {
    backgroundColor: 'transparent',
    grid: { left: 60, right: 60, top: 30, bottom: 40 },
    tooltip: { trigger: 'axis' },
    legend: { top: 0, data: ['gpu-hours', 'jobs running'] },
    xAxis: { type: 'category', data: xs, axisLabel: { rotate: 20 } },
    yAxis: [
      { type: 'value', name: 'gpu-hours' },
      { type: 'value', name: 'jobs', position: 'right' },
    ],
    series: [
      { name: 'gpu-hours', type: 'bar', data: ghs, itemStyle: { color: '#4096ff' } },
      { name: 'jobs running', type: 'line', yAxisIndex: 1, data: jobs, smooth: true, showSymbol: false, lineStyle: { color: '#faad14' } },
    ],
  };
});

useChart(resourcesChartEl, resourcesOption);

// ---- computed UI data ----

const headerCollaborators = computed(() => detail.value?.owners ?? detail.value?.collaborators ?? []);

/** Best metric display: handle both live {name,value} shape and legacy plain-number shape. */
const bestMetricDisplay = computed(() => {
  const bm = detail.value?.best_metric;
  if (bm == null) return null;
  const fallback = t('common.metric');
  if (typeof bm === 'object') {
    return { name: (bm as { name: string; value: number }).name ?? fallback, value: (bm as { name: string; value: number }).value };
  }
  // Legacy: plain number — use best_metric_key if available
  return { name: detail.value?.best_metric_key ?? fallback, value: bm as number };
});

const recentBatches = computed(() => {
  // Show done/failed batches sorted by end_time desc (skip the ones already in Active).
  const activeIds = new Set(active.value.map((a) => a.batch_id));
  return recent.value
    .filter((b) => !activeIds.has(b.id) && b.status !== 'running')
    .sort((a, b) => (b.end_time ?? b.start_time ?? '').localeCompare(a.end_time ?? a.start_time ?? ''));
});

// ---- lifecycle ----

onMounted(() => {
  void refreshAll();
});
onUnmounted(() => {
  if (autoRefreshTimer !== null) window.clearInterval(autoRefreshTimer);
});

// Reload when :project param changes.
watch(
  () => props.project,
  () => {
    detail.value = null;
    active.value = [];
    recent.value = [];
    leaderboard.value = [];
    matrix.value = null;
    resources.value = null;
    void refreshAll();
  },
);

// ---- actions ----

function openBatch(b: Batch) {
  // Stay inside the demo tree when mounted read-only; otherwise use the
  // authenticated /batches route.
  const prefix = canWrite.value ? '/batches/' : '/demo/batches/';
  router.push(prefix + encodeURIComponent(b.id));
}

function onShareRequest(batchId: string) {
  shareBatchId.value = batchId;
}

function duplicateConfig(batch: Batch) {
  // Placeholder — backend doesn't ship a retrigger endpoint in MVP.
  const cmd = batch.command;
  if (!cmd) return;
  if (navigator.clipboard?.writeText) {
    void navigator.clipboard.writeText(cmd);
  }
}

// Stable ML-default metric ordering. Unknown keys sort alphabetically after.
const METRIC_ORDER = [
  'MSE', 'MAE', 'RMSE', 'R2', 'PCC', 'SCC',
  'sMAPE', 'MAPE', 'MASE', 'RAE', 'MSPE',
  'Latency_P50', 'Latency_P95', 'Latency_P99',
  'GPU_Memory', 'CPU_Memory', 'Inference_Throughput',
  'Total_Train_Time', 'Avg_Epoch_Time', 'Avg_Batch_Time',
];

/**
 * Collect the union of all metric keys present across every leaderboard row,
 * then sort them according to METRIC_ORDER (known first, unknown alphabetically).
 */
const allMetricKeys = computed<string[]>(() => {
  const seen = new Set<string>();
  for (const row of leaderboard.value) {
    if (row.metrics) {
      for (const k of Object.keys(row.metrics)) {
        // Skip epoch/training-stat keys that we expose as dedicated columns.
        if (k !== 'train_epochs' && k !== 'epochs') {
          seen.add(k);
        }
      }
    }
  }
  const known = METRIC_ORDER.filter((k) => seen.has(k));
  const unknown = [...seen].filter((k) => !METRIC_ORDER.includes(k)).sort();
  return [...known, ...unknown];
});

const leaderboardColumns = computed(() => {
  const fixed = [
    { title: t('page_project_detail.col_batch'), dataIndex: 'batch_id', key: 'batch_id', width: 240 },
    { title: t('page_project_detail.col_model'), dataIndex: 'model', key: 'model', width: 140 },
    { title: t('page_project_detail.col_dataset'), dataIndex: 'dataset', key: 'dataset', width: 140 },
    { title: t('page_project_detail.col_status'), key: 'status', width: 100 },
    { title: t('page_project_detail.col_epochs'), key: 'train_epochs', dataIndex: 'train_epochs', width: 80 },
    { title: t('page_project_detail.col_elapsed'), key: 'elapsed', width: 100 },
  ];
  const metricCols = allMetricKeys.value.map((k) => ({
    title: k,
    key: `metric_${k}`,
    width: 110,
  }));
  return [...fixed, ...metricCols];
});
</script>

<template>
  <div class="page-container">
    <!-- Anonymous CTA shown above everything else. -->
    <AnonymousCTA v-if="isAnonymous" />

    <!-- Breadcrumb -->
    <a-breadcrumb style="margin-bottom: 10px">
      <template v-if="canWrite">
        <a-breadcrumb-item>
          <a href="#" @click.prevent="router.push('/')">{{ t('page_project_detail.breadcrumb_dashboard') }}</a>
        </a-breadcrumb-item>
        <a-breadcrumb-item>
          <a href="#" @click.prevent="router.push('/projects')">{{ t('page_project_detail.breadcrumb_projects') }}</a>
        </a-breadcrumb-item>
      </template>
      <template v-else>
        <a-breadcrumb-item>
          <a href="#" @click.prevent="router.push('/demo')">{{ t('page_public_project_list.demos') }}</a>
        </a-breadcrumb-item>
      </template>
      <a-breadcrumb-item>{{ project }}</a-breadcrumb-item>
    </a-breadcrumb>

    <!-- Header row 1 -->
    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap">
      <a-button size="small" @click="router.push(canWrite ? '/projects' : '/demo')">
        <template #icon><ArrowLeftOutlined /></template>
      </a-button>
      <div style="font-size: 20px; font-weight: 600">{{ project }}</div>

      <!-- Star is per-user; hide for anonymous / demo. -->
      <StarButton v-if="canWrite" target-type="project" :target-id="project" />

      <a-button size="small" @click="refreshAll(true)" :loading="loading">
        <template #icon><ReloadOutlined /></template>
        {{ t('common.refresh') }}
      </a-button>

      <span style="flex: 1" />

      <!-- Project share opens a "virtual" share dialog keyed on no batch; we reuse
           ShareDialog by passing a pseudo batch_id (project share tab is the one
           users care about). The dialog's batch tab will simply show empty.
           WRITE ACTION — hidden in read-only mode. -->
      <a-button
        v-if="canWrite"
        size="small"
        type="primary"
        ghost
        @click="shareBatchId = 'project-level'"
      >
        <template #icon><ShareAltOutlined /></template>
        {{ t('page_project_detail.share_project') }}
      </a-button>

      <a-button
        v-if="canWrite"
        size="small"
        @click="router.push('/batches?project=' + encodeURIComponent(project))"
      >
        <template #icon><LinkOutlined /></template>
        {{ t('page_project_detail.all_batches') }}
      </a-button>

      <a-popconfirm
        v-if="canWrite && isAdmin"
        :title="$t('common.confirm_delete_project')"
        :ok-text="$t('common.delete')"
        :cancel-text="$t('common.cancel')"
        ok-type="danger"
        @confirm="handleDeleteProject"
      >
        <a-button size="small" danger :loading="deleting">
          <template #icon><DeleteOutlined /></template>
          {{ $t('common.delete') }}
        </a-button>
      </a-popconfirm>
    </div>

    <!-- Header row 2: aggregate strip -->
    <a-card size="small" style="margin-bottom: 12px" :bodyStyle="{ padding: '10px 14px' }">
      <a-row :gutter="12">
        <a-col :xs="12" :sm="8" :md="4">
          <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ t('page_project_detail.stat_total_batches') }}</div>
          <div style="font-size: 18px; font-weight: 600">{{ detail?.n_batches ?? '—' }}</div>
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ t('page_project_detail.stat_this_week') }}</div>
          <div style="font-size: 18px; font-weight: 600">{{ detail?.batches_this_week ?? '—' }}</div>
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ t('page_project_detail.stat_failure_rate') }}</div>
          <div style="font-size: 18px; font-weight: 600">
            {{
              detail?.failure_rate != null
                ? `${((detail.failure_rate as number) * 100).toFixed(1)}%`
                : '—'
            }}
          </div>
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ t('page_project_detail.stat_gpu_hours') }}</div>
          <div style="font-size: 18px; font-weight: 600">
            {{
              detail?.gpu_hours != null
                ? (detail.gpu_hours as number).toFixed(1)
                : '—'
            }}
          </div>
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ t('page_project_detail.stat_best_metric') }}</div>
          <div style="font-size: 18px; font-weight: 600">
            <template v-if="bestMetricDisplay != null">
              {{ bestMetricDisplay.name }}
              = {{ typeof bestMetricDisplay.value === 'number' ? bestMetricDisplay.value.toFixed(4) : '—' }}
            </template>
            <span v-else>—</span>
          </div>
        </a-col>
        <a-col :xs="12" :sm="8" :md="4">
          <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ t('page_project_detail.stat_collaborators') }}</div>
          <div style="font-size: 13px; font-weight: 500; line-height: 28px">
            {{ headerCollaborators.length ? headerCollaborators.join(', ') : '—' }}
          </div>
        </a-col>
      </a-row>
    </a-card>

    <!-- Tabs -->
    <!-- Tab order (v0.1.3 density pass): outcomes-first.
         Leaderboard → Matrix → Active → Recent → Resources → Collaborators. -->
    <a-tabs :active-key="activeTab" @change="onTabChange">
      <!-- Leaderboard -->
      <a-tab-pane key="leaderboard" :tab="t('page_project_detail.tab_leaderboard')">
        <div style="display: flex; gap: 8px; margin-bottom: 10px; align-items: center; flex-wrap: wrap">
          <span style="flex: 1" />
          <ExportCsvButton
            :label="t('page_project_detail.export_leaderboard')"
            :handler="() => exportProjectCsv(project)"
          />
          <ExportCsvButton
            :label="t('page_project_detail.export_raw')"
            type="text"
            :handler="() => exportProjectRawCsv(project)"
          />
        </div>
        <a-table
          :columns="leaderboardColumns"
          :data-source="leaderboard"
          :loading="loading"
          row-key="job_id"
          size="small"
          :scroll="{ x: true }"
          :pagination="{ pageSize: 25 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'batch_id'">
              <a
                href="#"
                @click.prevent="
                  router.push(
                    (canWrite ? '/batches/' : '/demo/batches/')
                      + encodeURIComponent((record as LeaderboardRow).batch_id ?? ''),
                  )
                "
              >
                {{ (record as LeaderboardRow).batch_id }}
              </a>
            </template>
            <template v-else-if="column.key === 'status'">
              <StatusTag :status="(record as LeaderboardRow).status ?? undefined" />
            </template>
            <template v-else-if="column.key === 'train_epochs'">
              {{ (record as LeaderboardRow).train_epochs ?? '—' }}
            </template>
            <template v-else-if="column.key === 'elapsed'">
              {{ fmtDuration((record as LeaderboardRow).elapsed_s ?? null) }}
            </template>
            <template v-else-if="(column.key as string).startsWith('metric_')">
              {{ (() => {
                const metricKey = (column.key as string).slice('metric_'.length);
                const v = (record as LeaderboardRow).metrics?.[metricKey];
                return v != null ? (v as number).toFixed(4) : '—';
              })() }}
            </template>
          </template>
          <template #emptyText>
            <div class="muted empty-wrap">{{ t('page_project_detail.leaderboard_empty') }}</div>
          </template>
        </a-table>
      </a-tab-pane>

      <!-- Matrix -->
      <a-tab-pane key="matrix" :tab="t('page_project_detail.tab_matrix')">
        <div style="display: flex; gap: 8px; margin-bottom: 8px; align-items: center">
          <span>{{ t('common.metric') }}:</span>
          <a-select v-model:value="matrixMetric" style="width: 120px">
            <a-select-option value="MSE">MSE</a-select-option>
            <a-select-option value="MAE">MAE</a-select-option>
            <a-select-option value="R2">R²</a-select-option>
            <a-select-option value="PCC">PCC</a-select-option>
          </a-select>
        </div>
        <div v-if="!matrix" class="muted empty-wrap">
          {{ t('page_project_detail.matrix_empty') }}
        </div>
        <div
          v-else
          ref="matrixChartEl"
          :style="{ width: '100%', height: Math.max(260, matrix.models.length * 28 + 140) + 'px' }"
        />
      </a-tab-pane>

      <!-- Active -->
      <a-tab-pane key="active" :tab="t('page_project_detail.tab_active') + ' 🟢'">
        <div v-if="!active.length" class="muted empty-wrap">
          {{ t('page_project_detail.active_empty') }}
        </div>
        <a-row :gutter="[16, 16]">
          <a-col v-for="b in active" :key="b.batch_id" :xs="24" :md="12" :xl="8">
            <!-- @share only wired when we can actually open the share dialog. -->
            <BatchCard :data="b" @share="canWrite ? onShareRequest : undefined" />
          </a-col>
        </a-row>
      </a-tab-pane>

      <!-- Recent -->
      <a-tab-pane key="recent" :tab="t('page_project_detail.tab_recent')">
        <div v-if="!recentBatches.length" class="muted empty-wrap">
          {{ t('page_project_detail.recent_empty') }}
        </div>
        <a-row :gutter="[12, 12]">
          <a-col
            v-for="b in recentBatches"
            :key="b.id"
            :xs="24"
            :sm="12"
            :xl="8"
          >
            <a-card
              size="small"
              hoverable
              :bodyStyle="{ padding: '12px 14px' }"
              @click="openBatch(b)"
            >
              <div style="display: flex; align-items: center; gap: 6px; margin-bottom: 4px">
                <StatusTag :status="b.status" />
                <div
                  style="font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px;
                         min-width: 0; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis"
                >
                  {{ b.id }}
                </div>
              </div>
              <ProgressInline
                :done="b.n_done"
                :total="b.n_total"
                :failed="b.n_failed"
                :width="'100%'"
              />
              <div class="muted" style="font-size: 11px; margin-top: 4px">
                {{ b.user || '—' }} · {{ b.host || '—' }} ·
                {{ fmtRelative(b.end_time ?? b.start_time) }}
              </div>
              <div style="margin-top: 8px; display: flex; gap: 6px">
                <a-button size="small" @click.stop="openBatch(b)">{{ t('common.view') }}</a-button>
                <a-tooltip :title="t('page_project_detail.tooltip_copy_command')">
                  <a-button size="small" :disabled="!b.command" @click.stop="duplicateConfig(b)">
                    <template #icon><CopyOutlined /></template>
                    {{ t('common.duplicate') }}
                  </a-button>
                </a-tooltip>
                <!-- Share is a write action (creates public link). -->
                <a-button
                  v-if="canWrite"
                  size="small"
                  @click.stop="onShareRequest(b.id)"
                >
                  <template #icon><ShareAltOutlined /></template>
                </a-button>
              </div>
            </a-card>
          </a-col>
        </a-row>
      </a-tab-pane>

      <!-- Resources -->
      <a-tab-pane key="resources" :tab="t('page_project_detail.tab_resources')">
        <div v-if="!resources" class="muted empty-wrap">
          {{ t('page_project_detail.resources_empty') }}
        </div>
        <template v-else>
          <a-row :gutter="[12, 12]" style="margin-bottom: 12px">
            <a-col :xs="12" :md="6">
              <a-card size="small">
                <div class="muted" style="font-size: 11px; text-transform: uppercase">{{ t('page_project_detail.resources_total_gpu_hours') }}</div>
                <div style="font-size: 20px; font-weight: 600">
                  {{ resources.total_gpu_hours?.toFixed(1) ?? '—' }}
                </div>
              </a-card>
            </a-col>
            <a-col :xs="12" :md="6" v-for="h in resources.by_host ?? []" :key="h.host">
              <a-card size="small">
                <div class="muted" style="font-size: 11px; text-transform: uppercase">{{ h.host }}</div>
                <div style="font-size: 20px; font-weight: 600">{{ h.gpu_hours.toFixed(1) }}h</div>
              </a-card>
            </a-col>
          </a-row>
          <a-card size="small" :title="t('page_project_detail.gpu_hours_over_time')">
            <div ref="resourcesChartEl" style="width: 100%; height: 320px" />
          </a-card>
        </template>
      </a-tab-pane>

      <!-- Collaborators — management pane, hidden in read-only mode. -->
      <a-tab-pane v-if="canWrite" key="collaborators" :tab="t('page_project_detail.tab_collaborators')">
        <a-alert
          type="info"
          show-icon
          :message="t('page_project_detail.collaborators_info')"
          :description="t('page_project_detail.collaborators_info_desc')"
          style="margin-bottom: 12px"
        />
        <a-table
          :columns="[
            { title: t('page_project_detail.collaborators_col_user'), key: 'grantee_username', dataIndex: 'grantee_username' },
            { title: t('page_project_detail.collaborators_col_permission'), key: 'permission', width: 140 },
            { title: t('page_project_detail.collaborators_col_since'), key: 'created_at', width: 200 },
          ]"
          :data-source="collaborators"
          row-key="grantee_id"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'permission'">
              <a-tag :color="(record as ProjectShare).permission === 'editor' ? 'orange' : 'blue'">
                {{ (record as ProjectShare).permission }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'created_at'">
              {{ fmtRelative((record as ProjectShare).created_at) || '—' }}
            </template>
          </template>
          <template #emptyText>
            <div class="muted empty-wrap">{{ t('page_project_detail.collaborators_empty') }}</div>
          </template>
        </a-table>
        <div style="margin-top: 12px">
          <a-button type="primary" @click="shareBatchId = 'project-level'">
            <template #icon><ShareAltOutlined /></template>
            {{ t('page_project_detail.open_share_dialog') }}
          </a-button>
        </div>
      </a-tab-pane>

      <!-- Notifications — multi-recipient list (v0.1.4). Visible to anyone
           with read access; editing only allowed for the project owner /
           admin (the panel itself defers ownership to the backend, so
           non-owners get 403 on writes). -->
      <a-tab-pane key="notifications" :tab="t('page_project_detail.tab_notifications')">
        <ProjectRecipientsPanel :project="project" :can-edit="canWrite" />
      </a-tab-pane>
    </a-tabs>

    <!-- Share dialog is itself a write-capable surface; never render for demo. -->
    <ShareDialog
      v-if="shareBatchId && canWrite"
      v-model:open="shareOpen"
      :batch-id="shareBatchId"
      :project="project"
    />
  </div>
</template>
