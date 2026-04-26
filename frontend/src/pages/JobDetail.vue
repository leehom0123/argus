<script setup lang="ts">
/**
 * JobDetail — single-job inspector. Refactored under #104 from the
 * vertically-stacked design (header → metrics card → loss curve →
 * epoch table → logs drawer) to a denser layout that exposes telemetry
 * and live logs at a glance:
 *
 *   ┌────────────────────────────────────────────────────────────┐
 *   │ Sticky telemetry strip — 5 cells, 60px high                │
 *   ├──────────────────────────────────┬─────────────────────────┤
 *   │  Loss curve (dominant)           │  Embedded log tail      │
 *   │  Epoch metrics (collapsible)     │  (first-class panel)    │
 *   ├──────────────────────────────────┴─────────────────────────┤
 *   │ Action bar — Stop / Rerun / Share / Copy command           │
 *   └────────────────────────────────────────────────────────────┘
 *
 * On screens < lg the layout collapses to a single column with a tab
 * group (Curve / Logs) so logs remain reachable on tablets.
 *
 * Telemetry refresh: ResourceSnapshot poll @ 5s while the job is
 * running, plus the existing ETA poll @ 10s. The backend doesn't expose
 * a per-job resource SSE today, so polling is the lightest path that
 * keeps the strip fresh without backend changes.
 */
import {
  computed,
  defineAsyncComponent,
  onMounted,
  onUnmounted,
  ref,
  watch,
} from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import {
  ArrowLeftOutlined,
  CopyOutlined,
  DeleteOutlined,
  ReloadOutlined,
  ShareAltOutlined,
} from '@ant-design/icons-vue';
import { message } from 'ant-design-vue';
import {
  getJob,
  getJobEpochs,
  getJobEta,
  getResources,
  deleteJob,
  type JobEtaInfo,
} from '../api/client';
import type { EpochPoint, Job, ResourceSnapshot } from '../types';
import StatusTag from '../components/StatusTag.vue';
import JobIdleBadge from '../components/JobIdleBadge.vue';
import MetricsBar from '../components/MetricsBar.vue';
import AnonymousCTA from '../components/AnonymousCTA.vue';
import LogTailPanel from '../components/LogTailPanel.vue';
import ShareDialog from '../components/ShareDialog.vue';
import { usePermissions } from '../composables/usePermissions';
import { getStatusColor } from '../composables/useStatusColor';
import { fmtTime, durationBetween, fmtDuration } from '../utils/format';
import dayjs from 'dayjs';

// LossChart is the only echarts consumer on this page. Defer so the
// shell paints first; the curve fills in once the chart bundle loads.
const LossChart = defineAsyncComponent(() => import('../components/LossChart.vue'));

const { t } = useI18n();
const props = defineProps<{
  batchId: string;
  jobId: string;
  /** Route-propagated read-only flag when mounted via /demo/*. */
  readOnly?: boolean;
}>();
const router = useRouter();
const { canWrite, isAnonymous } = usePermissions(props.readOnly);

const job = ref<Job | null>(null);
const epochs = ref<EpochPoint[]>([]);
const loading = ref(false);
const jobEta = ref<JobEtaInfo | null>(null);
const snapshots = ref<ResourceSnapshot[]>([]);
let etaTimer: number | null = null;
let snapshotTimer: number | null = null;

// Responsive breakpoint — md is 768px (AntD). We use matchMedia so the
// component only re-renders when crossing the boundary, not on every
// resize tick.
const isCompact = ref(false);
let mediaQuery: MediaQueryList | null = null;
function updateCompact(ev: MediaQueryList | MediaQueryListEvent): void {
  isCompact.value = !ev.matches;
}

// ---------------------------------------------------------------------------
// Polling — ETA + resource snapshots while the job is running.
// ---------------------------------------------------------------------------

async function fetchEta(): Promise<void> {
  try {
    jobEta.value = await getJobEta(props.batchId, props.jobId);
  } catch {
    // non-critical; interceptor toasts
  }
}

async function fetchSnapshots(): Promise<void> {
  try {
    // The /resources endpoint is host-scoped; without a host filter we
    // get the freshest snapshots across all hosts and the strip uses
    // the latest one. Limit=5 is a conservative bound — we only need
    // the head of the list.
    const list = await getResources({ limit: 5 });
    snapshots.value = list ?? [];
  } catch {
    // interceptor toasts
  }
}

function startTimers(): void {
  stopTimers();
  if (job.value?.status !== 'running') return;
  etaTimer = window.setInterval(fetchEta, 10_000);
  snapshotTimer = window.setInterval(fetchSnapshots, 5_000);
  void fetchSnapshots();
}

function stopTimers(): void {
  if (etaTimer !== null) { window.clearInterval(etaTimer); etaTimer = null; }
  if (snapshotTimer !== null) { window.clearInterval(snapshotTimer); snapshotTimer = null; }
}

function fmtEta(etaS: number | null): string {
  if (etaS === null) return t('component_eta.warming_up');
  if (etaS <= 0) return '0s';
  return `~${fmtDuration(etaS)}`;
}

function etaFinishAt(etaIso: string | null): string {
  if (!etaIso) return '';
  return dayjs(etaIso).format('YYYY-MM-DD HH:mm:ss');
}

async function fetchAll(): Promise<void> {
  loading.value = true;
  try {
    const [j, es] = await Promise.all([
      getJob(props.batchId, props.jobId),
      getJobEpochs(props.batchId, props.jobId),
    ]);
    job.value = j;
    epochs.value = es ?? [];
    if (j.status === 'running') {
      void fetchEta();
      startTimers();
    } else {
      stopTimers();
    }
  } catch {
    // interceptor notifies
  } finally {
    loading.value = false;
  }
}

// ---------------------------------------------------------------------------
// Telemetry strip — 5 cells derived from job + snapshots + epochs.
// ---------------------------------------------------------------------------

const statusTokens = computed(() =>
  getStatusColor('job', job.value?.status, {
    isIdleFlagged: job.value?.is_idle_flagged ?? false,
  }),
);

const elapsedDisplay = computed<string>(() => {
  const elapsed = job.value?.elapsed_s;
  if (typeof elapsed === 'number' && elapsed >= 0) return fmtDuration(elapsed);
  return durationBetween(job.value?.start_time, job.value?.end_time) || '—';
});

const etaDisplay = computed<string>(() => {
  if (job.value?.status !== 'running') return '—';
  return fmtEta(jobEta.value?.eta_s ?? null);
});

const latestSnapshot = computed<ResourceSnapshot | null>(() => {
  if (!snapshots.value.length) return null;
  // /resources returns newest-first.
  return snapshots.value[0] ?? null;
});

const gpuUtilDisplay = computed<string>(() => {
  const v = latestSnapshot.value?.gpu_util_pct;
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  return `${v.toFixed(0)}%`;
});

const gpuMemPeakDisplay = computed<string>(() => {
  // Peak across the snapshots window; falls back to "—" when none.
  let peak = 0;
  for (const s of snapshots.value) {
    const v = s.gpu_mem_mb;
    if (typeof v === 'number' && v > peak) peak = v;
  }
  if (peak <= 0) return '—';
  if (peak >= 1024) return `${(peak / 1024).toFixed(1)} GB`;
  return `${peak.toFixed(0)} MB`;
});

const latestLossDisplay = computed<string>(() => {
  if (!epochs.value.length) return '—';
  // Prefer val_loss; fall back to train_loss; iterate from the tail to
  // skip nulls (some reporters emit train_loss only on even epochs).
  for (let i = epochs.value.length - 1; i >= 0; i--) {
    const e = epochs.value[i];
    const v = (typeof e.val_loss === 'number' && Number.isFinite(e.val_loss))
      ? e.val_loss
      : (typeof e.train_loss === 'number' && Number.isFinite(e.train_loss))
        ? e.train_loss
        : null;
    if (v !== null) return v.toFixed(4);
  }
  return '—';
});

// ---------------------------------------------------------------------------
// Action bar — Stop (delete), Share, Copy command, Rerun (TBD).
// ---------------------------------------------------------------------------

const deleting = ref(false);
const shareOpen = ref(false);

async function handleDelete(): Promise<void> {
  if (deleting.value) return;
  deleting.value = true;
  try {
    await deleteJob(props.batchId, props.jobId);
    router.push(`/batches/${encodeURIComponent(props.batchId)}`);
  } catch {
    // interceptor notifies
  } finally {
    deleting.value = false;
  }
}

const jobCommand = computed<string>(() => {
  // Prefer extra.command when reporters surface it; fall back to a
  // run-dir hint so users always have something copyable.
  const extra = (job.value?.extra ?? {}) as Record<string, unknown>;
  const cmd = extra.command;
  if (typeof cmd === 'string' && cmd.trim()) return cmd.trim();
  if (job.value?.run_dir) {
    return `# ${job.value.run_dir}`;
  }
  return '';
});

async function copyJobCommand(): Promise<void> {
  const text = jobCommand.value || `${props.batchId}/${props.jobId}`;
  try {
    await navigator.clipboard.writeText(text);
    message.success(t('page_job_detail.actions.copied'));
  } catch {
    message.error(t('page_job_detail.actions.copy_failed'));
  }
}

// ---------------------------------------------------------------------------
// Lifecycle — initial fetch, identity-change re-handshake, cleanup.
// ---------------------------------------------------------------------------

watch(
  () => [props.batchId, props.jobId],
  () => {
    snapshots.value = [];
    epochs.value = [];
    void fetchAll();
  },
);

onMounted(async () => {
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    mediaQuery = window.matchMedia('(min-width: 992px)');
    isCompact.value = !mediaQuery.matches;
    mediaQuery.addEventListener('change', updateCompact);
  }
  await fetchAll();
});
onUnmounted(() => {
  stopTimers();
  if (mediaQuery) {
    mediaQuery.removeEventListener('change', updateCompact);
    mediaQuery = null;
  }
});
</script>

<template>
  <div class="page-container job-detail-page">
    <AnonymousCTA v-if="isAnonymous" />

    <!-- Header row — back button, job id, refresh. Action buttons live
         in the bottom action bar; only navigation lives up here. -->
    <div class="job-detail-header">
      <a-button
        size="small"
        @click="router.push(
          (canWrite ? '/batches/' : '/demo/batches/') + encodeURIComponent(batchId),
        )"
      >
        <template #icon><ArrowLeftOutlined /></template>
        {{ t('page_job_detail.back_to_batch') }}
      </a-button>
      <div class="job-detail-title">{{ jobId }}</div>
      <a-button size="small" :loading="loading" @click="fetchAll">
        <template #icon><ReloadOutlined /></template>
      </a-button>
    </div>

    <!-- Telemetry strip — 5 cells, sticky to the page top while scrolling -->
    <div class="telemetry-strip" data-test="telemetry-strip">
      <div class="telemetry-cell" data-test="telemetry-status">
        <div class="telemetry-label">
          {{ t('page_job_detail.telemetry.status') }}
        </div>
        <div class="telemetry-value">
          <span
            class="telemetry-status-dot"
            :style="{ background: statusTokens.border }"
            :aria-label="statusTokens.aria"
          />
          <StatusTag :status="job?.status" />
          <JobIdleBadge :flagged="job?.is_idle_flagged" />
        </div>
      </div>

      <div class="telemetry-cell" data-test="telemetry-elapsed">
        <div class="telemetry-label">
          {{ t('page_job_detail.telemetry.elapsed_eta') }}
        </div>
        <div class="telemetry-value">
          <span class="telemetry-primary">{{ elapsedDisplay }}</span>
          <a-tooltip
            v-if="job?.status === 'running'"
            :title="jobEta?.eta_iso
              ? t('component_eta.hover_finish_at', { time: etaFinishAt(jobEta.eta_iso) })
              : ''"
          >
            <span class="telemetry-secondary">
              · {{ t('page_job_detail.telemetry.eta_short') }} {{ etaDisplay }}
            </span>
          </a-tooltip>
        </div>
      </div>

      <div class="telemetry-cell" data-test="telemetry-gpu-util">
        <div class="telemetry-label">
          {{ t('page_job_detail.telemetry.gpu_util') }}
        </div>
        <div class="telemetry-value">
          <span class="telemetry-primary">{{ gpuUtilDisplay }}</span>
        </div>
      </div>

      <div class="telemetry-cell" data-test="telemetry-gpu-mem">
        <div class="telemetry-label">
          {{ t('page_job_detail.telemetry.gpu_mem_peak') }}
        </div>
        <div class="telemetry-value">
          <span class="telemetry-primary">{{ gpuMemPeakDisplay }}</span>
        </div>
      </div>

      <div class="telemetry-cell" data-test="telemetry-loss">
        <div class="telemetry-label">
          {{ t('page_job_detail.telemetry.latest_loss') }}
        </div>
        <div class="telemetry-value">
          <span class="telemetry-primary">{{ latestLossDisplay }}</span>
        </div>
      </div>
    </div>

    <!-- Compact identity card — kept terse so the strip + curve dominate -->
    <a-card size="small" class="identity-card">
      <a-descriptions :column="isCompact ? 1 : 2" size="small" bordered>
        <a-descriptions-item :label="t('page_job_detail.desc_job_id')">
          {{ job?.id ?? jobId }}
        </a-descriptions-item>
        <a-descriptions-item :label="t('page_job_detail.desc_batch_id')">
          {{ job?.batch_id ?? batchId }}
        </a-descriptions-item>
        <a-descriptions-item :label="t('page_job_detail.desc_model')">
          {{ job?.model ?? '—' }}
        </a-descriptions-item>
        <a-descriptions-item :label="t('page_job_detail.desc_dataset')">
          {{ job?.dataset ?? '—' }}
        </a-descriptions-item>
        <a-descriptions-item :label="t('page_job_detail.desc_start')">
          {{ fmtTime(job?.start_time) }}
        </a-descriptions-item>
        <a-descriptions-item :label="t('page_job_detail.desc_end')">
          {{ fmtTime(job?.end_time) }}
        </a-descriptions-item>
        <a-descriptions-item
          v-if="job?.run_dir"
          :label="t('page_job_detail.desc_run_dir')"
          :span="isCompact ? 1 : 2"
        >
          <code class="run-dir">{{ job.run_dir }}</code>
        </a-descriptions-item>
      </a-descriptions>
    </a-card>

    <!-- Two-column main area on lg+; tabs fallback on md / sm -->
    <template v-if="isCompact">
      <a-tabs class="job-detail-tabs" data-test="job-detail-tabs">
        <a-tab-pane key="curve" :tab="t('page_job_detail.tab_curve')">
          <a-card size="small" :title="t('page_job_detail.card_loss_curve')">
            <LossChart v-if="epochs.length" :epochs="epochs" />
            <div v-else class="muted empty-wrap">
              {{ t('page_job_detail.loss_empty') }}
            </div>
          </a-card>
          <a-collapse class="metrics-accordion" :bordered="false">
            <a-collapse-panel
              key="metrics"
              :header="t('page_job_detail.card_final_metrics')"
            >
              <MetricsBar v-if="job?.metrics" :metrics="job.metrics" />
              <div v-else class="muted empty-wrap">
                {{ t('page_job_detail.metrics_empty') }}
              </div>
            </a-collapse-panel>
          </a-collapse>
        </a-tab-pane>
        <a-tab-pane key="logs" :tab="t('page_job_detail.tab_logs')">
          <LogTailPanel
            :batch-id="batchId"
            :job-id="jobId"
            height="600px"
          />
        </a-tab-pane>
      </a-tabs>
    </template>
    <template v-else>
      <a-row :gutter="16" class="main-row">
        <a-col :xs="24" :lg="14">
          <a-card size="small" :title="t('page_job_detail.card_loss_curve')">
            <LossChart v-if="epochs.length" :epochs="epochs" />
            <div v-else class="muted empty-wrap">
              {{ t('page_job_detail.loss_empty') }}
            </div>
          </a-card>
          <a-collapse class="metrics-accordion" :bordered="false">
            <a-collapse-panel
              key="metrics"
              :header="t('page_job_detail.card_final_metrics')"
            >
              <MetricsBar v-if="job?.metrics" :metrics="job.metrics" />
              <div v-else class="muted empty-wrap">
                {{ t('page_job_detail.metrics_empty') }}
              </div>
            </a-collapse-panel>
          </a-collapse>
        </a-col>
        <a-col :xs="24" :lg="10">
          <a-card
            size="small"
            :title="t('page_job_detail.card_logs')"
            :body-style="{ padding: 0 }"
            class="logs-card"
          >
            <LogTailPanel
              :batch-id="batchId"
              :job-id="jobId"
              height="560px"
            />
          </a-card>
        </a-col>
      </a-row>
    </template>

    <!-- Bottom action bar — Stop / Rerun (writeable), Share / Copy (read) -->
    <div class="action-bar" data-test="action-bar">
      <a-popconfirm
        v-if="canWrite"
        :title="t('common.confirm_delete_job')"
        :ok-text="t('common.delete')"
        :cancel-text="t('common.cancel')"
        ok-type="danger"
        @confirm="handleDelete"
      >
        <a-button danger :loading="deleting">
          <template #icon><DeleteOutlined /></template>
          {{ t('page_job_detail.actions.stop') }}
        </a-button>
      </a-popconfirm>
      <a-button
        v-if="canWrite"
        @click="router.push(`/batches/${encodeURIComponent(batchId)}?rerun=1`)"
      >
        <template #icon><ReloadOutlined /></template>
        {{ t('page_job_detail.actions.rerun') }}
      </a-button>
      <span class="action-spacer" />
      <a-button @click="shareOpen = true">
        <template #icon><ShareAltOutlined /></template>
        {{ t('page_job_detail.actions.share') }}
      </a-button>
      <a-button @click="copyJobCommand">
        <template #icon><CopyOutlined /></template>
        {{ t('page_job_detail.actions.copy_command') }}
      </a-button>
    </div>

    <ShareDialog v-model:open="shareOpen" :batch-id="batchId" />
  </div>
</template>

<style scoped>
.job-detail-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.job-detail-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.job-detail-title {
  font-size: 16px;
  font-weight: 500;
  flex: 1;
}

.telemetry-strip {
  position: sticky;
  top: 0;
  z-index: 5;
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 1px;
  background: var(--surface-soft-strong);
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  overflow: hidden;
}

.telemetry-cell {
  background: var(--telemetry-bg, #fafafa);
  padding: 8px 12px;
  height: 60px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
}

.telemetry-label {
  font-size: 11px;
  color: rgba(0, 0, 0, 0.55);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.telemetry-value {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.telemetry-status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
}

.telemetry-primary { font-weight: 600; }
.telemetry-secondary { color: rgba(0, 0, 0, 0.55); font-size: 12px; }

.identity-card { /* compact card */ }
.run-dir {
  word-break: break-all;
  font-family: monospace;
  font-size: 11px;
}

.main-row { /* placeholder for layout overrides */ }
.metrics-accordion { margin-top: 8px; }

.logs-card :deep(.ant-card-body) {
  padding: 0;
  height: 560px;
}

.job-detail-tabs :deep(.ant-tabs-content) {
  min-height: 400px;
}

.action-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: #fafafa;
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: 6px;
}

.action-spacer { flex: 1; }

/* #120: previously this used ``@media (prefers-color-scheme: dark)`` which
   keys off the OS preference, not the user's in-app theme toggle. The
   dark / light flip is driven by ``html.dark`` (see store/app.ts), so we
   match on that instead. ``:global()`` escapes the scoped style hash so
   the html ancestor selector resolves. */
:global(html.dark) .telemetry-cell { background: #1f1f1f; }
:global(html.dark) .telemetry-label { color: rgba(255, 255, 255, 0.55); }
:global(html.dark) .telemetry-secondary { color: rgba(255, 255, 255, 0.55); }
:global(html.dark) .action-bar {
  background: #1f1f1f;
  border-color: rgba(255, 255, 255, 0.08);
}
</style>
