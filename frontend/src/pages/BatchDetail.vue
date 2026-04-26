<script setup lang="ts">
import { defineAsyncComponent, onMounted, onUnmounted, ref, watch, computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import { ArrowLeftOutlined, DeleteOutlined, RedoOutlined, ReloadOutlined, ShareAltOutlined } from '@ant-design/icons-vue';
import {
  getBatch,
  listJobs,
  getBatchResources,
  getBatchLogLines,
  getBatchEpochsLatest,
  getBatchJobsEtaAll,
  stopBatch,
  deleteBatch,
  bulkDeleteJobs,
  getBatchEmailSubscription,
  setBatchEmailSubscription,
  clearBatchEmailSubscription,
  type LogLine,
  type JobEpochLatest,
  type JobEtaInfo,
} from '../api/client';
import { useAuthStore } from '../store/auth';
import { message } from 'ant-design-vue';
import { exportBatchCsv } from '../api/exports';
import type { Batch, Job, ResourceSnapshot } from '../types';
import StatusTag from '../components/StatusTag.vue';
import JobIdleBadge from '../components/JobIdleBadge.vue';
import ProgressInline from '../components/ProgressInline.vue';
import JobMatrix from '../components/JobMatrix.vue';
import ShareDialog from '../components/ShareDialog.vue';
import PinButton from '../components/PinButton.vue';
import StarButton from '../components/StarButton.vue';
import ExportCsvButton from '../components/ExportCsvButton.vue';
import MiniSparkline from '../components/MiniSparkline.vue';
import ReproChipRow from '../components/ReproChipRow.vue';
import RerunModal from '../components/RerunModal.vue';
import AnonymousCTA from '../components/AnonymousCTA.vue';
import { usePermissions } from '../composables/usePermissions';
import { peek, invalidate, cacheKey } from '../composables/useCache';
import { useMultiSSE } from '../composables/useMultiSSE';
import { fmtTime, durationBetween, fmtRelative, fmtDuration } from '../utils/format';
import { statusBorderColor } from '../utils/status';
import dayjs from 'dayjs';

// ResourceChart pulls in echarts; not rendered until the Resources tab is
// visible. Defer so the BatchDetail shell + matrix/jobs tabs render without
// the chart bundle cost.
const ResourceChart = defineAsyncComponent(() => import('../components/ResourceChart.vue'));

const { t } = useI18n();
const props = defineProps<{
  batchId: string;
  /** When true mount in read-only mode (hides stop / rerun / share / pin / star / export). */
  readOnly?: boolean;
}>();
const router = useRouter();
const route = useRoute();
const { canWrite, isAnonymous } = usePermissions(props.readOnly);

// State for "show all metrics" modal.
const metricsModalOpen = ref(false);
const metricsModalJob = ref<Job | null>(null);

function openMetricsModal(job: Job) {
  metricsModalJob.value = job;
  metricsModalOpen.value = true;
}

/** Sorted metric entries from a job for the modal table. */
function allMetricEntries(m: Job['metrics']): { key: string; value: number }[] {
  if (!m) return [];
  return Object.entries(m)
    .filter(([, v]) => typeof v === 'number')
    .map(([key, value]) => ({ key, value: value as number }))
    .sort((a, b) => a.key.localeCompare(b.key));
}

// Stale-while-revalidate: seed from the cache if we prefetched via hover
// on the previous page. Lets the header (status/name/user/host) render
// instantly while the real /batches/{id} + /jobs calls settle in the
// background.
const batch = ref<Batch | null>(peek<Batch>(cacheKey.batchSummary(props.batchId)));
const jobs = ref<Job[]>([]);
const loading = ref(batch.value === null);
const stopping = ref(false);

/** Map from job_id → per-job ETA, fetched via bulk endpoint. */
const jobEtaMap = ref<Record<string, JobEtaInfo>>({});

/** Format ETA seconds into a compact relative string (~12m, ~2h 34m). */
function fmtEta(etaS: number | null): string {
  if (etaS === null) return t('component_eta.warming_up');
  if (etaS <= 0) return '0s';
  return `~${fmtDuration(etaS)}`;
}

/** Absolute finish time for tooltip. */
function etaFinishAt(etaIso: string | null): string {
  if (!etaIso) return '';
  return dayjs(etaIso).format('YYYY-MM-DD HH:mm:ss');
}

async function fetchEtaAll() {
  try {
    jobEtaMap.value = await getBatchJobsEtaAll(props.batchId);
  } catch {
    // non-critical; silently ignore
  }
}

async function handleStop() {
  if (!batch.value || stopping.value) return;
  stopping.value = true;
  try {
    await stopBatch(props.batchId);
    batch.value = { ...batch.value, status: 'stopping' };
  } catch {
    // Interceptor already showed a notification.
  } finally {
    stopping.value = false;
  }
}

const selectedJobIds = ref<string[]>([]);
const bulkDeletingJobs = ref(false);

const jobRowSelection = computed(() => ({
  selectedRowKeys: selectedJobIds.value,
  onChange: (keys: (string | number)[]) => {
    selectedJobIds.value = keys.map(String);
  },
}));

async function runBulkDeleteJobs() {
  if (!selectedJobIds.value.length || bulkDeletingJobs.value) return;
  bulkDeletingJobs.value = true;
  try {
    const ids = selectedJobIds.value.slice();
    const res = await bulkDeleteJobs(
      ids.map((j) => ({ batch_id: props.batchId, job_id: j })),
    );
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
    selectedJobIds.value = [];
    await fetchAll();
  } catch {
    // interceptor notifies
  } finally {
    bulkDeletingJobs.value = false;
  }
}

const deleting = ref(false);

async function handleDelete() {
  if (deleting.value) return;
  deleting.value = true;
  try {
    await deleteBatch(props.batchId);
    router.push('/batches');
  } catch {
    // Interceptor already showed a notification.
  } finally {
    deleting.value = false;
  }
}

// Live-panel state
const resources = ref<ResourceSnapshot[]>([]);
const logLines = ref<LogLine[]>([]);
const epochsLatest = ref<JobEpochLatest[]>([]);

type BatchDetailTab =
  | 'matrix'
  | 'jobs'
  | 'timeline'
  | 'resources'
  | 'training'
  | 'logs'
  | 'notifications';

// Default tab can be overridden via ?tab= query (ProjectDetail active cards
// deep-link into matrix / jobs).
function initialTab(): BatchDetailTab {
  const tabParam = String(route.query.tab ?? '');
  if (
    tabParam === 'matrix' ||
    tabParam === 'jobs' ||
    tabParam === 'timeline' ||
    tabParam === 'resources' ||
    tabParam === 'training' ||
    tabParam === 'logs' ||
    tabParam === 'notifications'
  ) {
    return tabParam;
  }
  return 'matrix';
}
const activeTab = ref<BatchDetailTab>(initialTab());

// ── Notifications tab state ─────────────────────────────────────────────
// "Email me when..." per-batch override.  Visible only to the owner;
// non-owners never see the tab so the 403 path is never exercised from
// the UI.  ``hasOverride`` distinguishes "fresh batch — defaults"
// (404 from GET) from "owner has saved an override" (200 with row).
const auth = useAuthStore();
const isOwner = computed(() => {
  const u = auth.currentUser?.username;
  if (!u || !batch.value) return false;
  return batch.value.user === u;
});
type EmailEventKind = 'batch_done' | 'batch_failed' | 'job_failed' | 'batch_diverged' | 'job_idle_flagged';
const NOTIFY_EVENT_KINDS: EmailEventKind[] = [
  'batch_done',
  'batch_failed',
  'job_failed',
  'batch_diverged',
  'job_idle_flagged',
];
const notifySelected = ref<EmailEventKind[]>([]);
const notifyHasOverride = ref(false);
const notifyLoading = ref(false);
const notifySaving = ref(false);
const notifyResetting = ref(false);

async function loadNotifySettings() {
  if (!isOwner.value) return;
  notifyLoading.value = true;
  try {
    const row = await getBatchEmailSubscription(props.batchId);
    if (row) {
      notifyHasOverride.value = true;
      // Only keep kinds we recognise — server may add new kinds in
      // the future and we don't want to render unknown checkboxes.
      notifySelected.value = row.event_kinds.filter((k): k is EmailEventKind =>
        (NOTIFY_EVENT_KINDS as string[]).includes(k),
      );
    } else {
      notifyHasOverride.value = false;
      notifySelected.value = [];
    }
  } catch {
    // interceptor already toasted
  } finally {
    notifyLoading.value = false;
  }
}

async function saveNotifySettings() {
  if (notifySaving.value) return;
  notifySaving.value = true;
  try {
    await setBatchEmailSubscription(props.batchId, {
      event_kinds: notifySelected.value,
      enabled: true,
    });
    notifyHasOverride.value = true;
    message.success(t('batch.notify_saved'));
  } catch {
    // interceptor toasts
  } finally {
    notifySaving.value = false;
  }
}

async function resetNotifySettings() {
  if (notifyResetting.value) return;
  notifyResetting.value = true;
  try {
    await clearBatchEmailSubscription(props.batchId);
    notifyHasOverride.value = false;
    notifySelected.value = [];
    message.success(t('batch.notify_saved'));
  } catch {
    // interceptor toasts
  } finally {
    notifyResetting.value = false;
  }
}
const shareOpen = ref(false);
const rerunOpen = ref(false);
let timer: number | null = null;
// Multiplexed SSE — replaces the old per-batch `/api/events/stream`
// EventSource. The composable manages the EventSource lifecycle; we
// flip ``sseChannels`` to start / stop the subscription. Today we
// consume one channel (the batch); when JobDetail-inline panels land,
// the same connection will absorb ``job:<batch>:<job>`` selectors with
// no extra HTTP connection.
const sseChannels = ref<string[]>([]);

const jobCols = computed(() => [
  { title: t('page_batch_detail.col_job_id'), dataIndex: 'id', key: 'id', width: 280, fixed: 'left' as const },
  { title: t('page_batch_detail.col_model'), dataIndex: 'model', key: 'model', width: 140 },
  { title: t('page_batch_detail.col_dataset'), dataIndex: 'dataset', key: 'dataset', width: 140 },
  { title: t('page_batch_detail.col_status'), key: 'status', width: 110 },
  { title: t('page_batch_detail.col_start'), key: 'start_time', width: 180 },
  { title: t('page_batch_detail.col_elapsed'), key: 'elapsed', width: 100 },
  { title: t('page_batch_detail.col_eta'), key: 'eta', width: 110 },
  { title: t('page_batch_detail.col_metrics'), key: 'metrics' },
]);

async function fetchAll() {
  loading.value = true;
  try {
    const [b, js] = await Promise.all([getBatch(props.batchId), listJobs(props.batchId)]);
    batch.value = b;
    jobs.value = js ?? [];
    // Drop the stale summary — any prefetch from another page is superseded
    // by the full record we just fetched.
    invalidate(cacheKey.batchSummary(props.batchId));
    // Also refresh ETA map when job list refreshes.
    fetchEtaAll();
  } catch {
    // interceptor already notified
  } finally {
    loading.value = false;
  }
}

let etaTimer: number | null = null;

function startEtaTimer() {
  stopEtaTimer();
  if (batch.value?.status === 'running') {
    etaTimer = window.setInterval(fetchEtaAll, 10_000);
  }
}

function stopEtaTimer() {
  if (etaTimer !== null) { window.clearInterval(etaTimer); etaTimer = null; }
}

async function fetchLivePanels() {
  try {
    const [resResp, logs, epochs] = await Promise.all([
      getBatchResources(props.batchId),
      getBatchLogLines(props.batchId),
      getBatchEpochsLatest(props.batchId),
    ]);
    resources.value = resResp.snapshots ?? [];
    // Populate batch.host from resources response if not already set.
    if (resResp.host && batch.value && !batch.value.host) {
      batch.value = { ...batch.value, host: resResp.host };
    }
    logLines.value = logs ?? [];
    epochsLatest.value = epochs ?? [];
  } catch {
    // non-critical; silently ignore
  } finally {
    // Always flip — even on failure — so the empty-state's "0 lines" copy
    // can render instead of leaving the user on a perpetual blank panel.
    logsFetched.value = true;
  }
}

/** Refresh live panels when an SSE event arrives for this batch. */
function refreshOnSseEvent(evType: string) {
  if (evType === 'resource_snapshot') {
    getBatchResources(props.batchId).then((r) => { resources.value = r.snapshots ?? []; }).catch(() => {});
  } else if (evType === 'job_epoch') {
    getBatchEpochsLatest(props.batchId).then((e) => { epochsLatest.value = e ?? []; }).catch(() => {});
  } else if (evType === 'log_line') {
    getBatchLogLines(props.batchId).then((l) => { logLines.value = l ?? []; }).catch(() => {});
  }
  if (['batch_done', 'batch_failed', 'job_done', 'job_failed', 'job_start'].includes(evType)) {
    fetchAll();
  }
}

// Set up the multiplexed connection up front. The composable opens /
// closes the EventSource based on the channels ref; ``startSse`` /
// ``stopSse`` just toggle the channel list. We pre-register the same
// event types the old wiring used so frame-driven refresh handlers
// stay identical.
useMultiSSE(sseChannels, {
  onMessage: (_channel, eventType) => {
    const handled = [
      'resource_snapshot', 'job_epoch', 'log_line',
      'batch_done', 'batch_failed', 'job_done', 'job_failed', 'job_start',
    ];
    if (handled.includes(eventType)) {
      refreshOnSseEvent(eventType);
    }
  },
});

function startSse() {
  // Idempotent — useMultiSSE only reopens the underlying EventSource
  // when the channel list actually changes.
  const token = localStorage.getItem('argus.access_token');
  if (!token) return;
  sseChannels.value = [`batch:${props.batchId}`];
}

function stopSse() {
  sseChannels.value = [];
}

function startTimer() {
  stopTimer();
  if (batch.value?.status === 'running') {
    timer = window.setInterval(() => { fetchAll(); fetchLivePanels(); }, 15000);
  }
}
function stopTimer() {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

watch(() => batch.value?.status, (status) => {
  startTimer();
  startEtaTimer();
  if (status === 'running') {
    startSse();
  } else {
    stopSse();
  }
});

function openJob(job: Job) {
  // Keep read-only viewers inside the /demo tree for consistent chrome/banner.
  const prefix = canWrite.value ? '/batches/' : '/demo/batches/';
  router.push(
    `${prefix}${encodeURIComponent(props.batchId)}/jobs/${encodeURIComponent(job.id)}`,
  );
}

function metricsSummary(m?: Job['metrics']): string {
  if (!m) return '—';
  const parts: string[] = [];
  for (const k of ['MSE', 'MAE', 'R2', 'PCC']) {
    const v = m[k];
    if (typeof v === 'number') parts.push(`${k}=${v.toFixed(3)}`);
  }
  return parts.length ? parts.join(' · ') : '—';
}

const sortedJobs = computed(() => {
  return [...jobs.value].sort((a, b) => (a.start_time ?? '').localeCompare(b.start_time ?? ''));
});

/** Level → Ant Design tag colour */
function logLevelColor(level?: string | null): string {
  if (level === 'error') return 'red';
  if (level === 'warning' || level === 'warn') return 'orange';
  if (level === 'debug') return 'default';
  return 'blue';
}

/** Latest resource snapshot for the gauge row. */
const latestSnap = computed(() => resources.value[resources.value.length - 1] ?? null);

// ── Host chip row helpers (mirrors BatchCompactCard color logic) ──────────

function utilColor(pct: number | null): string {
  if (pct == null) return 'default';
  if (pct < 60) return 'green';
  if (pct < 80) return 'gold';
  if (pct < 90) return 'orange';
  return 'red';
}

function tempColor(c: number | null): string {
  if (c == null) return 'default';
  if (c < 70) return 'green';
  if (c < 80) return 'gold';
  return 'red';
}

function ratioColor(used: number | null, total: number | null): string {
  if (used == null || total == null || total === 0) return 'default';
  return utilColor(Math.round((used / total) * 100));
}

function fmtGB(mb?: number | null, totalMb?: number | null): string {
  if (mb == null) return '—';
  const used = (mb / 1024).toFixed(1);
  if (totalMb != null) return `${used}/${(totalMb / 1024).toFixed(1)} GB`;
  return `${used} GB`;
}

function fmtDiskGB(mb?: number | null): string {
  if (mb == null) return '—';
  return `${(mb / 1024).toFixed(1)} GB`;
}

const snapPid = computed<number | null>(() => {
  const snap = latestSnap.value;
  if (!snap) return null;
  const v = (snap as Record<string, unknown>).pid;
  return typeof v === 'number' ? v : null;
});

// ── Logs tab state ────────────────────────────────────────────────────────

const logSearchText = ref('');
const logLevelFilter = ref<string>('all');
const logsAutoRefresh = ref(true);
// Tracks whether the *first* /log-lines fetch has completed.  Until it
// finishes the empty-state would otherwise flash "no log lines" before the
// list paints.  Flipped to true inside fetchLivePanels (success or fail).
const logsFetched = ref(false);
const logsRefreshing = ref(false);
let logsTimer: number | null = null;

// Manual refresh — bypasses the 10 s server-side cache via ?bust=<now>.
async function refreshLogs(): Promise<void> {
  if (logsRefreshing.value) return;
  logsRefreshing.value = true;
  try {
    const fresh = await getBatchLogLines(props.batchId, 200, true);
    logLines.value = fresh ?? [];
    logsFetched.value = true;
  } catch {
    // interceptor handles toast
  } finally {
    logsRefreshing.value = false;
  }
}

const filteredLogLines = computed(() => {
  let lines = [...logLines.value];
  if (logLevelFilter.value !== 'all') {
    lines = lines.filter((l) => {
      const lv = (l.level ?? '').toLowerCase();
      if (logLevelFilter.value === 'warn') return lv === 'warning' || lv === 'warn';
      return lv === logLevelFilter.value;
    });
  }
  if (logSearchText.value.trim()) {
    const q = logSearchText.value.trim().toLowerCase();
    lines = lines.filter((l) => (l.message ?? '').toLowerCase().includes(q));
  }
  return lines;
});

function startLogsTimer() {
  if (logsTimer !== null) window.clearInterval(logsTimer);
  logsTimer = window.setInterval(() => {
    if (activeTab.value === 'logs' && logsAutoRefresh.value) {
      getBatchLogLines(props.batchId).then((l) => { logLines.value = l ?? []; }).catch(() => {});
    }
  }, 10000);
}

function stopLogsTimer() {
  if (logsTimer !== null) { window.clearInterval(logsTimer); logsTimer = null; }
}

watch(logsAutoRefresh, (on) => { if (on) startLogsTimer(); else stopLogsTimer(); });

onMounted(async () => {
  // Kick off the batch-summary + live-panel fetches in parallel — they hit
  // independent endpoints (batch/jobs vs resources/log-lines/epochs). Before
  // this the two awaits ran back-to-back and the Resources/Training tabs
  // sat empty for an extra RTT.
  await Promise.all([fetchAll(), fetchLivePanels()]);
  if (batch.value?.status === 'running') { startSse(); startEtaTimer(); }
  startLogsTimer();
  // Notifications tab loads lazily — only when the owner actually
  // opens it (or deep-links via ?tab=notifications) so we don't burn
  // an RTT for non-owners or visitors hopping straight to /jobs.
  if (activeTab.value === 'notifications' && isOwner.value) {
    await loadNotifySettings();
  }
});

// When the user clicks into Notifications later, fetch on demand.
watch(activeTab, async (tab) => {
  if (tab === 'notifications' && isOwner.value) {
    await loadNotifySettings();
  }
});
onUnmounted(() => { stopTimer(); stopSse(); stopLogsTimer(); stopEtaTimer(); });
</script>

<template>
  <div class="page-container">
    <!-- Anonymous CTA shown above the breadcrumb when the visitor has no session. -->
    <AnonymousCTA v-if="isAnonymous" />

    <a-breadcrumb style="margin-bottom: 8px">
      <template v-if="canWrite">
        <a-breadcrumb-item>
          <a href="#" @click.prevent="router.push('/')">{{ $t('page_batch_detail.breadcrumb_dashboard') }}</a>
        </a-breadcrumb-item>
        <a-breadcrumb-item>
          <a href="#" @click.prevent="router.push('/projects')">{{ $t('page_batch_detail.breadcrumb_projects') }}</a>
        </a-breadcrumb-item>
        <a-breadcrumb-item v-if="batch?.project">
          <a href="#" @click.prevent="router.push(`/projects/${encodeURIComponent(batch?.project ?? '')}`)">
            {{ batch?.project }}
          </a>
        </a-breadcrumb-item>
      </template>
      <template v-else>
        <a-breadcrumb-item>
          <a href="#" @click.prevent="router.push('/demo')">{{ $t('page_public_project_list.demos') }}</a>
        </a-breadcrumb-item>
        <a-breadcrumb-item v-if="batch?.project">
          <a href="#" @click.prevent="router.push(`/demo/projects/${encodeURIComponent(batch?.project ?? '')}`)">
            {{ batch?.project }}
          </a>
        </a-breadcrumb-item>
      </template>
      <a-breadcrumb-item>{{ batchId }}</a-breadcrumb-item>
    </a-breadcrumb>

    <div
      :style="{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        marginBottom: '12px',
        flexWrap: 'wrap',
        borderLeft: `4px solid ${statusBorderColor(batch?.status)}`,
        paddingLeft: '10px',
      }"
    >
      <!-- Back goes to /batches for authed users, /demo for anonymous. -->
      <a-button size="small" @click="router.push(canWrite ? '/batches' : '/demo')">
        <template #icon><ArrowLeftOutlined /></template>
        {{ $t('page_batch_detail.back') }}
      </a-button>
      <div style="font-size: 16px; font-weight: 500">{{ batchId }}</div>
      <a-button size="small" :loading="loading" @click="fetchAll">
        <template #icon><ReloadOutlined /></template>
      </a-button>
      <span style="flex: 1" />
      <!-- WRITE-ACTION cluster: Star, Pin, Export, Share, Rerun — all
           hidden in read-only mode. A signed-in account is required for each. -->
      <template v-if="canWrite">
        <StarButton target-type="batch" :target-id="batchId" />
        <PinButton :batch-id="batchId" />
        <ExportCsvButton :handler="() => exportBatchCsv(batchId)" />
        <a-button size="small" type="primary" ghost @click="shareOpen = true">
          <template #icon><ShareAltOutlined /></template>
          {{ $t('page_batch_detail.share') }}
        </a-button>
        <a-button size="small" @click="rerunOpen = true">
          <template #icon><RedoOutlined /></template>
          {{ $t('component_rerun_modal.btn_open', 'Rerun') }}
        </a-button>
        <a-popconfirm
          :title="$t('common.confirm_delete_batch')"
          :ok-text="$t('common.delete')"
          :cancel-text="$t('common.cancel')"
          ok-type="danger"
          @confirm="handleDelete"
        >
          <a-button size="small" danger :loading="deleting">
            <template #icon><DeleteOutlined /></template>
            {{ $t('common.delete') }}
          </a-button>
        </a-popconfirm>
      </template>
    </div>

    <!-- Share + rerun modals are writeable surfaces; only mount them for users
         who can actually use them. -->
    <template v-if="canWrite">
      <ShareDialog
        v-model:open="shareOpen"
        :batch-id="batchId"
        :project="batch?.project ?? null"
      />

      <RerunModal
        v-model:open="rerunOpen"
        :batch-id="batchId"
        :source-name="batch?.id ?? null"
        :source-command="batch?.command ?? null"
        @rerun-created="(id: string) => router.push(`/batches/${id}`)"
      />
    </template>

    <!-- Host telemetry chip row -->
    <div
      v-if="latestSnap || batch?.host"
      style="display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 12px"
    >
      <a-tag v-if="batch?.host" color="default" style="font-size: 11px; line-height: 18px; padding: 0 6px">
        🖥 {{ $t('page_batch_detail.host_chip_row.host') }}: {{ batch.host }}
      </a-tag>
      <a-tag v-if="snapPid != null" color="default" style="font-size: 11px; line-height: 18px; padding: 0 6px">
        💻 {{ $t('page_batch_detail.host_chip_row.pid') }}: {{ snapPid }}
      </a-tag>
      <a-tag
        v-if="latestSnap?.gpu_util_pct != null"
        :color="utilColor(latestSnap.gpu_util_pct)"
        style="font-size: 11px; line-height: 18px; padding: 0 6px"
      >
        ⚡ GPU {{ Math.round(latestSnap.gpu_util_pct) }}%
      </a-tag>
      <a-tag
        v-if="latestSnap?.gpu_mem_mb != null"
        :color="ratioColor(latestSnap.gpu_mem_mb, latestSnap.gpu_mem_total_mb ?? null)"
        style="font-size: 11px; line-height: 18px; padding: 0 6px"
      >
        🧮 VRAM {{ fmtGB(latestSnap.gpu_mem_mb, latestSnap.gpu_mem_total_mb) }}
      </a-tag>
      <a-tag
        v-if="latestSnap?.cpu_util_pct != null"
        :color="utilColor(latestSnap.cpu_util_pct)"
        style="font-size: 11px; line-height: 18px; padding: 0 6px"
      >
        🧠 CPU {{ Math.round(latestSnap.cpu_util_pct) }}%
      </a-tag>
      <a-tag
        v-if="latestSnap?.ram_mb != null"
        :color="ratioColor(latestSnap.ram_mb, latestSnap.ram_total_mb ?? null)"
        style="font-size: 11px; line-height: 18px; padding: 0 6px"
      >
        🗄 RAM {{ fmtGB(latestSnap.ram_mb, latestSnap.ram_total_mb) }}
      </a-tag>
      <a-tag
        v-if="latestSnap?.disk_free_mb != null"
        color="default"
        style="font-size: 11px; line-height: 18px; padding: 0 6px"
      >
        💾 {{ fmtDiskGB(latestSnap.disk_free_mb) }} free
      </a-tag>
      <a-tag
        v-if="latestSnap?.gpu_temp_c != null"
        :color="tempColor(latestSnap.gpu_temp_c)"
        style="font-size: 11px; line-height: 18px; padding: 0 6px"
      >
        🌡 {{ Math.round(latestSnap.gpu_temp_c) }}°C
      </a-tag>
      <!-- Host-detail link requires an authenticated view of /hosts/:host. -->
      <a
        v-if="batch?.host && canWrite"
        :href="`/hosts/${encodeURIComponent(batch.host)}`"
        style="font-size: 11px; margin-left: 4px"
      >{{ $t('page_batch_detail.host_chip_row.view_host_detail') }} →</a>
    </div>

    <!-- Reproducibility chips: git SHA / python / deps / Hydra config -->
    <ReproChipRow v-if="batch?.env_snapshot" :env-snapshot="batch.env_snapshot" />

    <!-- All-metrics modal -->
    <a-modal
      v-model:open="metricsModalOpen"
      :title="t('page_batch_detail.jobs.show_all_metrics')"
      :footer="null"
      width="480"
    >
      <div v-if="metricsModalJob">
        <div class="muted" style="font-size: 12px; margin-bottom: 8px">
          {{ metricsModalJob.id }}
          <span v-if="metricsModalJob.model"> · {{ metricsModalJob.model }}</span>
          <span v-if="metricsModalJob.dataset"> · {{ metricsModalJob.dataset }}</span>
        </div>
        <a-table
          :data-source="allMetricEntries(metricsModalJob.metrics)"
          :columns="[
            { title: t('page_batch_detail.jobs.metrics_key'), dataIndex: 'key', key: 'key', width: 200 },
            { title: t('page_batch_detail.jobs.metrics_value'), dataIndex: 'value', key: 'value' },
          ]"
          :pagination="false"
          size="small"
          row-key="key"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'value'">
              {{ (record as Record<string, unknown>)['value'] != null ? Number((record as Record<string, unknown>)['value']).toFixed(4) : '—' }}
            </template>
          </template>
          <template #emptyText>
            <span class="muted">{{ t('page_batch_detail.jobs.no_metrics_yet') }}</span>
          </template>
        </a-table>
      </div>
    </a-modal>

    <a-card size="small" style="margin-bottom: 16px">
      <a-descriptions :column="2" size="small" bordered>
        <a-descriptions-item :label="$t('page_batch_detail.desc_id')">{{ batch?.id ?? batchId }}</a-descriptions-item>
        <a-descriptions-item :label="$t('page_batch_detail.desc_project')">{{ batch?.project ?? '—' }}</a-descriptions-item>
        <a-descriptions-item :label="$t('page_batch_detail.desc_user')">{{ batch?.user ?? '—' }}</a-descriptions-item>
        <a-descriptions-item :label="$t('page_batch_detail.desc_host')">{{ batch?.host ?? '—' }}</a-descriptions-item>
        <a-descriptions-item :label="$t('page_batch_detail.desc_type')">{{ batch?.experiment_type ?? '—' }}</a-descriptions-item>
        <a-descriptions-item :label="$t('page_batch_detail.desc_status')">
          <StatusTag :status="batch?.status" />
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_batch_detail.desc_start')">
          {{ fmtTime(batch?.start_time) }}
          <span class="muted" style="font-size: 11px; margin-left: 6px">
            {{ fmtRelative(batch?.start_time) }}
          </span>
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_batch_detail.desc_duration')">
          {{ durationBetween(batch?.start_time, batch?.end_time) }}
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_batch_detail.desc_command')" :span="2">
          <code style="white-space: pre-wrap; word-break: break-all">{{
            batch?.command ?? '—'
          }}</code>
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_batch_detail.desc_progress')" :span="2">
          <ProgressInline
            v-if="batch"
            :done="batch.n_done"
            :total="batch.n_total"
            :failed="batch.n_failed"
            :width="360"
          />
          <span v-else>—</span>
        </a-descriptions-item>
      </a-descriptions>

      <!-- Action row: Stop / Retry are WRITE actions — hidden for read-only viewers. -->
      <div v-if="canWrite" style="margin-top: 12px; display: flex; gap: 8px">
        <template v-if="batch?.status === 'running' || batch?.status === 'stopping'">
          <a-popconfirm
            v-if="batch?.status === 'running'"
            :title="$t('page_batch_detail.stop_confirm')"
            :ok-text="$t('page_batch_detail.stop_confirm_ok')"
            placement="bottomLeft"
            @confirm="handleStop"
          >
            <a-button size="small" danger :loading="stopping">
              {{ $t('page_batch_detail.stop_batch_button') }}
            </a-button>
          </a-popconfirm>
          <a-button v-else-if="batch?.status === 'stopping'" size="small" danger disabled>
            {{ $t('page_batch_detail.stopping_status') }}
          </a-button>
        </template>
        <a-button v-else size="small" disabled>{{ $t('page_batch_detail.btn_stop') }}</a-button>
        <!-- TODO: per-job retry isn't wired in the backend yet (no /jobs/{id}/retry).
             The "Retry failed" placeholder button was removed in v0.1.3 (UI density
             pass) so users don't try clicking a dead control. Restore once the
             retry endpoint lands. -->
      </div>
    </a-card>

    <!-- Tab order (v0.1.3 density pass): operator-first.
         Jobs → Matrix → Training → Logs → Timeline → Resources → Notifications. -->
    <a-tabs v-model:active-key="activeTab">
      <a-tab-pane key="jobs" :tab="$t('page_batch_detail.tab_jobs')">
        <div v-if="canWrite && selectedJobIds.length > 0" style="margin-bottom: 8px">
          <a-popconfirm
            :title="$t('common.bulk_delete_confirm', { n: selectedJobIds.length })"
            :ok-text="$t('common.delete')"
            :cancel-text="$t('common.cancel')"
            ok-type="danger"
            @confirm="runBulkDeleteJobs"
          >
            <a-button danger size="small" :loading="bulkDeletingJobs">
              <template #icon><DeleteOutlined /></template>
              {{ $t('common.bulk_delete_button', { n: selectedJobIds.length }) }}
            </a-button>
          </a-popconfirm>
        </div>
        <a-table
          :columns="jobCols"
          :data-source="sortedJobs"
          row-key="id"
          size="small"
          :scroll="{ x: 1200 }"
          :pagination="{ pageSize: 20 }"
          :row-selection="canWrite ? jobRowSelection : undefined"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'id'">
              <a @click.prevent="openJob(record as Job)" href="#">{{ (record as Job).id }}</a>
            </template>
            <template v-else-if="column.key === 'status'">
              <StatusTag :status="(record as Job).status" />
              <JobIdleBadge :flagged="(record as Job).is_idle_flagged" />
            </template>
            <template v-else-if="column.key === 'start_time'">
              {{ fmtTime((record as Job).start_time) }}
            </template>
            <template v-else-if="column.key === 'elapsed'">
              {{
                (record as Job).elapsed_s != null
                  ? `${(record as Job).elapsed_s}s`
                  : durationBetween((record as Job).start_time, (record as Job).end_time)
              }}
            </template>
            <template v-else-if="column.key === 'eta'">
              <template v-if="(record as Job).status === 'running'">
                <a-tooltip
                  :title="jobEtaMap[(record as Job).id]?.eta_iso
                    ? t('component_eta.hover_finish_at', { time: etaFinishAt(jobEtaMap[(record as Job).id]?.eta_iso ?? null) })
                    : ''"
                >
                  <span style="font-size: 12px">
                    {{ fmtEta(jobEtaMap[(record as Job).id]?.eta_s ?? null) }}
                  </span>
                </a-tooltip>
              </template>
              <span v-else class="muted" style="font-size: 12px">—</span>
            </template>
            <template v-else-if="column.key === 'metrics'">
              <span v-if="!(record as Job).metrics" class="muted" style="font-size: 12px">
                {{ t('page_batch_detail.jobs.no_metrics_yet') }}
              </span>
              <span v-else style="font-size: 12px">
                {{ metricsSummary((record as Job).metrics) }}
                <a-button
                  type="link"
                  size="small"
                  style="padding: 0 0 0 4px; font-size: 11px"
                  @click.stop="openMetricsModal(record as Job)"
                >{{ t('page_batch_detail.jobs.show_all_metrics') }}</a-button>
              </span>
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <a-tab-pane key="matrix" :tab="$t('page_batch_detail.tab_matrix')">
        <JobMatrix :jobs="jobs" :experiment-name="batch?.project ?? null" @pick="openJob" />
      </a-tab-pane>

      <!-- (b) Training progress panel -->
      <a-tab-pane key="training" :tab="$t('page_batch_detail.tab_training')">
        <template v-if="epochsLatest.length">
          <a-table
            :data-source="epochsLatest"
            row-key="job_id"
            size="small"
            :pagination="false"
            :scroll="{ x: 700 }"
          >
            <a-table-column key="job_id" :title="$t('page_batch_detail.training_col_job')" data-index="job_id" :width="200">
              <template #default="{ record }">
                <a
                  href="#"
                  @click.prevent="router.push(
                    `${canWrite ? '/batches/' : '/demo/batches/'}${encodeURIComponent(batchId)}/jobs/${encodeURIComponent((record as JobEpochLatest).job_id)}`,
                  )"
                >{{ (record as JobEpochLatest).job_id }}</a>
              </template>
            </a-table-column>
            <a-table-column key="epoch" :title="$t('page_batch_detail.training_col_epoch')" data-index="epoch" :width="80" />
            <a-table-column key="train_loss" :title="$t('page_batch_detail.training_col_train_loss')" :width="110">
              <template #default="{ record }">
                {{ (record as JobEpochLatest).train_loss != null ? (record as JobEpochLatest).train_loss!.toFixed(4) : '—' }}
              </template>
            </a-table-column>
            <a-table-column key="val_loss" :title="$t('page_batch_detail.training_col_val_loss')" :width="110">
              <template #default="{ record }">
                {{ (record as JobEpochLatest).val_loss != null ? (record as JobEpochLatest).val_loss!.toFixed(4) : '—' }}
              </template>
            </a-table-column>
            <a-table-column key="lr" :title="$t('page_batch_detail.training_col_lr')" :width="110">
              <template #default="{ record }">
                {{ (record as JobEpochLatest).lr != null ? (record as JobEpochLatest).lr!.toExponential(2) : '—' }}
              </template>
            </a-table-column>
            <a-table-column key="sparkline" :title="$t('page_batch_detail.training_col_sparkline')" :width="120">
              <template #default="{ record }">
                <MiniSparkline
                  :data="(record as JobEpochLatest).val_loss_trace ?? []"
                  :height="28"
                  color="#4096ff"
                  :area="true"
                />
              </template>
            </a-table-column>
          </a-table>
          <div class="muted" style="font-size: 11px; margin-top: 4px; text-align: right">
            {{ $t('page_batch_detail.training_footer') }}
          </div>
        </template>
        <div v-else class="muted empty-wrap">
          {{ $t('page_batch_detail.training_empty') }}
        </div>
      </a-tab-pane>

      <!-- (c) Logs panel -->
      <a-tab-pane key="logs" :tab="$t('page_batch_detail.tab_logs')">
        <!-- Filter row -->
        <div style="display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; align-items: center">
          <a-input-search
            v-model:value="logSearchText"
            :placeholder="$t('page_batch_detail.logs.filter_placeholder')"
            allow-clear
            size="small"
            style="width: 240px"
          />
          <a-select
            v-model:value="logLevelFilter"
            size="small"
            style="width: 120px"
          >
            <a-select-option value="all">{{ $t('page_batch_detail.logs.level_all') }}</a-select-option>
            <a-select-option value="error">{{ $t('page_batch_detail.logs.level_error') }}</a-select-option>
            <a-select-option value="warn">{{ $t('page_batch_detail.logs.level_warn') }}</a-select-option>
            <a-select-option value="info">{{ $t('page_batch_detail.logs.level_info') }}</a-select-option>
            <a-select-option value="debug">{{ $t('page_batch_detail.logs.level_debug') }}</a-select-option>
          </a-select>
          <a-button
            size="small"
            :loading="logsRefreshing"
            @click="refreshLogs"
          >{{ $t('page_batch_detail.logs.refresh_button') }}</a-button>
          <span style="flex: 1" />
          <span class="muted" style="font-size: 11px">
            {{ $t('page_batch_detail.logs.count_label', { shown: filteredLogLines.length, total: logLines.length }) }}
          </span>
          <a-switch v-model:checked="logsAutoRefresh" size="small" />
          <span class="muted" style="font-size: 11px">{{ $t('page_batch_detail.logs.auto_refresh_toggle') }}</span>
        </div>

        <template v-if="filteredLogLines.length">
          <div
            style="
              background: #1a1a1a;
              border-radius: 6px;
              padding: 10px 14px;
              font-family: monospace;
              font-size: 12px;
              max-height: 480px;
              overflow-y: auto;
              line-height: 1.6;
            "
          >
            <div
              v-for="line in filteredLogLines"
              :key="line.id"
              :style="{
                color:
                  line.level === 'error'
                    ? '#ff7875'
                    : line.level === 'warning' || line.level === 'warn'
                      ? '#ffa940'
                      : line.level === 'debug'
                        ? '#8c8c8c'
                        : '#d9d9d9',
              }"
            >
              <span style="color: #595959; margin-right: 6px">{{ fmtTime(line.timestamp) }}</span>
              <a-tag
                v-if="line.level"
                :color="logLevelColor(line.level)"
                style="margin-right: 6px; font-size: 10px; line-height: 16px; padding: 0 4px"
              >{{ line.level }}</a-tag>
              <span v-if="line.job_id" style="color: #69b1ff; margin-right: 6px">[{{ line.job_id }}]</span>
              {{ line.message ?? '' }}
            </div>
          </div>
          <div class="muted" style="font-size: 11px; margin-top: 4px; text-align: right">
            {{ $t('page_batch_detail.logs_footer') }}
          </div>
        </template>
        <div
          v-else-if="!logsFetched"
          class="muted empty-wrap"
          style="padding: 32px 0; text-align: center"
        >
          <a-spin />
          <div style="font-size: 12px; margin-top: 10px">
            {{ $t('page_batch_detail.logs.loading') }}
          </div>
        </div>
        <div v-else class="muted empty-wrap" style="padding: 32px 0; text-align: center">
          <div style="font-size: 14px; margin-bottom: 8px">{{ $t('page_batch_detail.logs.empty_title') }}</div>
          <div style="font-size: 12px; max-width: 520px; margin: 0 auto; line-height: 1.6">
            {{ $t('page_batch_detail.logs.empty_description') }}
          </div>
          <div style="font-size: 11px; margin-top: 8px; max-width: 520px; margin-left: auto; margin-right: auto; line-height: 1.6">
            {{ $t('page_batch_detail.logs.empty_legacy_note') }}
          </div>
          <div style="font-size: 11px; margin-top: 10px; color: #595959; font-family: monospace">
            {{ $t('page_batch_detail.logs.empty_hint') }}
          </div>
          <div style="margin-top: 14px">
            <a-button size="small" :loading="logsRefreshing" @click="refreshLogs">
              {{ $t('page_batch_detail.logs.refresh_button') }}
            </a-button>
          </div>
        </div>
      </a-tab-pane>

      <a-tab-pane key="timeline" :tab="$t('page_batch_detail.tab_timeline')">
        <a-timeline v-if="sortedJobs.length">
          <a-timeline-item
            v-for="j in sortedJobs"
            :key="j.id"
            :color="
              j.status === 'done'
                ? 'green'
                : j.status === 'running'
                  ? 'blue'
                  : j.status === 'failed'
                    ? 'red'
                    : 'gray'
            "
          >
            <div style="font-weight: 500">
              <a @click.prevent="openJob(j)" href="#">{{ j.id }}</a>
              <StatusTag :status="j.status" style="margin-left: 6px" />
              <JobIdleBadge :flagged="j.is_idle_flagged" />
            </div>
            <div class="muted" style="font-size: 12px">
              {{ fmtTime(j.start_time) }} ·
              {{
                j.elapsed_s != null
                  ? `${j.elapsed_s}s`
                  : durationBetween(j.start_time, j.end_time)
              }}
              · {{ metricsSummary(j.metrics) }}
            </div>
          </a-timeline-item>
        </a-timeline>
        <div v-else class="muted empty-wrap">{{ $t('page_batch_detail.timeline_no_events') }}</div>
      </a-tab-pane>

      <!-- Resources panel -->
      <a-tab-pane key="resources" :tab="$t('page_batch_detail.tab_resources')">
        <template v-if="resources.length">
          <!-- Latest snapshot summary row -->
          <div style="display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; align-items: center">
            <a-tag v-if="batch?.host" color="default" style="font-size: 11px">
              💻 {{ $t('page_batch_detail.resources_host') }}: {{ batch.host }}
            </a-tag>
            <a-tag
              v-if="(latestSnap as Record<string, unknown>)?.pid != null"
              color="default"
              style="font-size: 11px"
            >
              🔢 {{ $t('page_batch_detail.resources_pid') }}: {{ (latestSnap as Record<string, unknown>).pid }}
            </a-tag>
            <a-tag v-if="latestSnap?.gpu_temp_c != null" color="red" style="font-size: 11px">
              🌡️ {{ Math.round(latestSnap.gpu_temp_c) }}°C
            </a-tag>
          </div>
          <a-row :gutter="12" style="margin-bottom: 16px">
            <a-col :xs="12" :sm="6">
              <a-statistic
                :title="$t('page_batch_detail.resources_gpu_util')"
                :value="latestSnap?.gpu_util_pct ?? '—'"
                :suffix="latestSnap?.gpu_util_pct != null ? '%' : ''"
              />
            </a-col>
            <a-col :xs="12" :sm="6">
              <a-statistic
                :title="$t('page_batch_detail.resources_vram_used')"
                :value="latestSnap?.gpu_mem_mb != null ? `${Math.round(latestSnap.gpu_mem_mb)} / ${Math.round(latestSnap.gpu_mem_total_mb ?? 0)} MB` : '—'"
              />
            </a-col>
            <a-col :xs="12" :sm="6">
              <a-statistic
                :title="$t('page_batch_detail.resources_cpu_util')"
                :value="latestSnap?.cpu_util_pct ?? '—'"
                :suffix="latestSnap?.cpu_util_pct != null ? '%' : ''"
              />
            </a-col>
            <a-col :xs="12" :sm="6">
              <a-statistic
                :title="$t('page_batch_detail.resources_ram_used')"
                :value="latestSnap?.ram_mb != null ? `${Math.round(latestSnap.ram_mb)} / ${Math.round(latestSnap.ram_total_mb ?? 0)} MB` : '—'"
              />
            </a-col>
          </a-row>
          <ResourceChart :snapshots="resources" :height="300" />
          <div class="muted" style="font-size: 11px; margin-top: 4px; text-align: right">
            {{ $t('page_batch_detail.resources_footer', { host: batch?.host ?? '?', count: resources.length }) }}
          </div>
        </template>
        <div v-else class="muted empty-wrap">
          {{ $t('page_batch_detail.resources_empty') }}
        </div>
      </a-tab-pane>

      <!-- Notifications tab — owner only.  Hidden via v-if so the
           tab strip itself shrinks for everyone else (no greyed-out
           pane); the per-batch override is meaningless for project
           collaborators since they fall back to project defaults. -->
      <a-tab-pane
        v-if="isOwner"
        key="notifications"
        :tab="$t('batch.notifications_tab')"
      >
        <div style="max-width: 520px; padding: 8px 0">
          <div
            class="muted"
            style="font-size: 13px; margin-bottom: 14px; font-weight: 500"
          >
            {{ $t('batch.notify_when') }}
          </div>
          <a-spin :spinning="notifyLoading">
            <a-checkbox-group
              v-model:value="notifySelected"
              style="display: flex; flex-direction: column; gap: 10px"
            >
              <a-checkbox value="batch_done">{{ $t('batch.notify_batch_done') }}</a-checkbox>
              <a-checkbox value="batch_failed">{{ $t('batch.notify_batch_failed') }}</a-checkbox>
              <a-checkbox value="job_failed">{{ $t('batch.notify_job_failed') }}</a-checkbox>
              <a-checkbox value="batch_diverged">{{ $t('batch.notify_diverged') }}</a-checkbox>
              <a-checkbox value="job_idle_flagged">{{ $t('batch.notify_idle') }}</a-checkbox>
            </a-checkbox-group>
            <div style="margin-top: 18px; display: flex; gap: 8px">
              <a-button
                type="primary"
                :loading="notifySaving"
                @click="saveNotifySettings"
              >
                {{ $t('batch.notify_save') }}
              </a-button>
              <a-button
                v-if="notifyHasOverride"
                :loading="notifyResetting"
                @click="resetNotifySettings"
              >
                {{ $t('batch.notify_reset') }}
              </a-button>
            </div>
          </a-spin>
        </div>
      </a-tab-pane>
    </a-tabs>
  </div>
</template>
