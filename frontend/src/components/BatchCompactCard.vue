<script setup lang="ts">
/**
 * BatchCompactCard — TUI-density card for running batches.
 *
 * Replaces BatchCard in list-view / Dashboard "Running" contexts.
 * Target: ≤200px vertical so three batches fit above the fold on 1080p.
 *
 * Data is loaded by the caller via useBatchCompactData and passed in as a prop,
 * or loaded internally when only batchId is supplied.
 */
import { computed, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  MinusOutlined,
  ShareAltOutlined,
  CloseCircleFilled,
  WarningFilled,
  ThunderboltOutlined,
  DatabaseOutlined,
} from '@ant-design/icons-vue';
import { fmtDuration, fmtTime } from '../utils/format';
import MiniSparkline from './MiniSparkline.vue';
import ShareDialog from './ShareDialog.vue';
import { useBatchCompactData } from '../composables/useBatchCompactData';
import type { BatchCompactData } from '../composables/useBatchCompactData';
import { stopBatch } from '../api/client';
import type { JobEpochLatest } from '../api/client';
import { statusBorderColor } from '../utils/status';

const props = defineProps<{
  /** Either supply a batchId (card fetches its own data) ... */
  batchId?: string;
  /** ... or supply pre-fetched data directly (avoids double-fetch in lists). */
  compactData?: BatchCompactData;
  /**
   * Increment (or change) this to trigger a background refresh of internally-
   * fetched data. Dashboard passes `dash.lastFetchedAt` so the card refreshes
   * whenever the dashboard auto-refresh fires.
   */
  refreshKey?: number | string | null;
}>();

const { t } = useI18n();
const router = useRouter();

// If compactData is provided externally, skip internal fetch.
const internalHook = props.batchId && !props.compactData
  ? useBatchCompactData(props.batchId)
  : null;

const loading = computed(() => internalHook?.loading.value ?? false);
const cd = computed<BatchCompactData | null>(() => props.compactData ?? internalHook?.data.value ?? null);

// Re-fetch whenever the parent signals a refresh (e.g. dashboard auto-refresh tick).
watch(
  () => props.refreshKey,
  (newKey, oldKey) => {
    if (newKey !== undefined && newKey !== oldKey && internalHook) {
      void internalHook.refresh();
    }
  },
);

// ── Derived fields ────────────────────────────────────────────────────────

const batchId = computed(() => cd.value?.batch.id ?? props.batchId ?? '');
const status = computed(() => cd.value?.batch.status ?? 'pending');
const project = computed(() => cd.value?.batch.project ?? '');
const user = computed(() => cd.value?.batch.user ?? '');
const host = computed(() => cd.value?.batch.host ?? '');
const nTotal = computed(() => cd.value?.batch.n_total ?? 0);
const nDone = computed(() => cd.value?.batch.n_done ?? 0);
const nFailed = computed(() => cd.value?.batch.n_failed ?? 0);
const etaSec = computed(() => cd.value?.eta?.eta_seconds ?? null);

// Elapsed from batch.start_time
const elapsedSec = computed(() => {
  const start = cd.value?.batch.start_time;
  if (!start) return null;
  return Math.floor((Date.now() - Date.parse(start)) / 1000);
});

const progressPct = computed(() =>
  nTotal.value > 0 ? Math.round((nDone.value / nTotal.value) * 100) : 0,
);

// ── Running slots ─────────────────────────────────────────────────────────

const MAX_VISIBLE_SLOTS = 3;

interface SlotInfo {
  jobId: string;
  label: string;
  epochStr: string;
  valLoss: string;
  trend: 'down' | 'up' | 'flat' | null;
  trace: number[];
}

function epochStr(e: JobEpochLatest): string {
  // We don't have total_epochs from this endpoint; show just current epoch.
  return `ep ${e.epoch}`;
}

function trendOf(trace: (number | null)[]): 'down' | 'up' | 'flat' | null {
  const vals = trace.filter((v): v is number => v !== null && v !== undefined);
  if (vals.length < 2) return null;
  const last = vals[vals.length - 1];
  const prev = vals[vals.length - 2];
  if (last < prev) return 'down';
  if (last > prev) return 'up';
  return 'flat';
}

const runningJobs = computed(() => {
  const jobs = cd.value?.jobs ?? [];
  const epochs = cd.value?.epochsLatest ?? [];
  const epochMap = new Map<string, JobEpochLatest>(epochs.map((e) => [e.job_id, e]));

  return jobs
    .filter((j) => j.status === 'running')
    .map((j): SlotInfo => {
      const ep = epochMap.get(j.id);
      const trace = (ep?.val_loss_trace ?? []).filter((v): v is number => v !== null) as number[];
      const trend = trendOf(ep?.val_loss_trace ?? []);
      const model = j.model ?? '—';
      const dataset = j.dataset ?? '—';
      return {
        jobId: j.id,
        label: `${model} × ${dataset}`,
        epochStr: ep ? epochStr(ep) : '',
        valLoss: ep?.val_loss != null ? ep.val_loss.toFixed(4) : '',
        trend,
        trace,
      };
    });
});

const visibleSlots = computed(() => runningJobs.value.slice(0, MAX_VISIBLE_SLOTS));
const extraCount = computed(() => Math.max(0, runningJobs.value.length - MAX_VISIBLE_SLOTS));

// ── Resources (last snapshot) ─────────────────────────────────────────────

const lastResource = computed(() => {
  const rs = cd.value?.resources ?? [];
  return rs.length > 0 ? rs[rs.length - 1] : null;
});

const hasSnapshots = computed(() => (cd.value?.resources ?? []).length > 0);

const gpuUtil = computed(() => lastResource.value?.gpu_util_pct ?? null);
const gpuMemMb = computed(() => lastResource.value?.gpu_mem_mb ?? null);
const gpuMemTotalMb = computed(() => lastResource.value?.gpu_mem_total_mb ?? null);
const gpuTempC = computed(() => lastResource.value?.gpu_temp_c ?? null);
const cpuUtil = computed(() => lastResource.value?.cpu_util_pct ?? null);
const ramMb = computed(() => lastResource.value?.ram_mb ?? null);
const ramTotalMb = computed(() => lastResource.value?.ram_total_mb ?? null);
const diskFreeMb = computed(() => lastResource.value?.disk_free_mb ?? null);
const diskTotalMb = computed(() => lastResource.value?.disk_total_mb ?? null);
const pid = computed<number | null>(() => {
  const snap = lastResource.value;
  if (!snap) return null;
  // pid can be stored directly on the snapshot (normalised by client.ts) or in extra.
  const v = (snap as Record<string, unknown>).pid;
  return typeof v === 'number' ? v : null;
});

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

// ── Sparkline: per-batch progress trace ───────────────────────────────────
// We don't have a historical n_done time-series — that field is overwritten
// in place on each batch_progress event, not appended. The most progress-y
// signal we *do* have is the per-job val_loss_trace from epoch_end events:
// epochs only advance when training makes forward progress, so the running
// length of these traces is a faithful "how far along is the sweep" proxy.
//
// We emit the union of val_loss values across every running job (oldest →
// newest, padded right by zero so the bar goes up monotonically as work
// completes). When no training data exists yet (sweep still warming up,
// or data-pipeline-only batches with no epoch_end events) we fall back to
// the GPU-util trace so the row still renders something live.
const sparklineData = computed<number[]>(() => {
  const epochs = cd.value?.epochsLatest ?? [];
  // Concatenate every job's val_loss_trace into one long monotone-ish
  // series. Drop nulls; ordering is "first-job-first" which is fine
  // for a 60×18px sparkline that's all about the trend silhouette.
  const merged: number[] = [];
  for (const e of epochs) {
    const trace = e.val_loss_trace ?? [];
    for (const v of trace) {
      if (v != null && Number.isFinite(v)) merged.push(v);
    }
  }
  if (merged.length >= 2) {
    return merged.slice(-20);
  }
  // Fallback: GPU util — keeps the bar non-empty when no training
  // events have arrived yet. Labelled "Progress" because it still
  // tracks "is anything alive at all" rather than steady-state util.
  const rs = cd.value?.resources ?? [];
  return rs
    .map((r) => r.gpu_util_pct)
    .filter((v): v is number => v != null)
    .slice(-20);
});

// ── Stalled detection ─────────────────────────────────────────────────────

const isStalled = computed(() => cd.value?.health?.is_stalled ?? false);
const stalledAgeS = computed(() => cd.value?.health?.last_event_age_s ?? null);

// ── Color helpers for telemetry chips ────────────────────────────────────

/** Map a utilization percentage to an ant-design tag color string. */
function utilColor(pct: number | null): string {
  if (pct == null) return 'default';
  if (pct < 60) return 'green';
  if (pct < 80) return 'gold';
  if (pct < 90) return 'orange';
  return 'red';
}

/** Map a GPU temperature to an ant-design tag color string. */
function tempColor(c: number | null): string {
  if (c == null) return 'default';
  if (c < 70) return 'green';
  if (c < 80) return 'gold';
  return 'red';
}

/**
 * Map a used/total ratio to a color string.
 * Identical to utilColor but named separately for clarity.
 */
function ratioColor(used: number | null, total: number | null): string {
  if (used == null || total == null || total === 0) return 'default';
  return utilColor(Math.round((used / total) * 100));
}

/** Disk color: use usage% when total is known, else absolute free GB threshold. */
function diskColor(freeMb: number | null, totalMb: number | null): string {
  if (freeMb == null) return 'default';
  if (totalMb != null && totalMb > 0) {
    const usedPct = Math.round(((totalMb - freeMb) / totalMb) * 100);
    return utilColor(usedPct);
  }
  const freeGB = freeMb / 1024;
  if (freeGB < 10) return 'red';
  if (freeGB < 30) return 'orange';
  return 'green';
}

// ── Trend icon helpers ─────────────────────────────────────────────────────

function trendIcon(trend: 'down' | 'up' | 'flat' | null) {
  if (trend === 'down') return ArrowDownOutlined;
  if (trend === 'up') return ArrowUpOutlined;
  return MinusOutlined;
}
function trendColor(trend: 'down' | 'up' | 'flat' | null) {
  if (trend === 'down') return '#52c41a';
  if (trend === 'up') return '#ff4d4f';
  // Flat trend → neutral text token (theme-aware) instead of literal white (#120).
  return 'var(--text-tertiary)';
}

// ── Status dot ────────────────────────────────────────────────────────────

const statusDot = computed(() => {
  const s = status.value;
  if (s === 'running') return '🟢';
  if (s === 'pending') return '🟡';
  if (s === 'done') return '✅';
  if (s === 'failed') return '🔴';
  if (s === 'stopping') return '🛑';
  return '⚪';
});

/** True while the batch status is 'stopping'. */
const isStopping = computed(() => status.value === 'stopping');

// ── Actions ───────────────────────────────────────────────────────────────

function viewMatrix(ev: MouseEvent) {
  ev.stopPropagation();
  router.push(`/batches/${encodeURIComponent(batchId.value)}?tab=matrix`);
}

function viewJobs(ev: MouseEvent) {
  ev.stopPropagation();
  router.push(`/batches/${encodeURIComponent(batchId.value)}?tab=jobs`);
}

const shareOpen = ref(false);

function openShare(ev: MouseEvent) {
  ev.stopPropagation();
  shareOpen.value = true;
}

// Stop button: POST /api/batches/{id}/stop.
const stopping = ref(false);

async function handleStop() {
  if (stopping.value) return;
  stopping.value = true;
  try {
    await stopBatch(batchId.value);
    // Optimistically update the local status so the chip flips immediately
    // without waiting for the next polling cycle.
    if (cd.value) {
      (cd.value.batch as { status: string }).status = 'stopping';
    }
  } catch {
    // Interceptor already displayed a notification.
  } finally {
    stopping.value = false;
  }
}
</script>

<template>
  <a-card
    size="small"
    :body-style="{
      padding: '8px 12px',
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      gap: '6px',
    }"
    :style="{
      borderLeft: `4px solid ${statusBorderColor(status)}`,
      borderRadius: '6px',
      height: '100%',
    }"
  >
    <!-- Row 1: chip row — status dot, batch id, user, host -->
    <div class="cbc-row cbc-chip-row">
      <span class="cbc-status-dot">{{ statusDot }}</span>
      <span class="cbc-batch-id">{{ batchId }}</span>
      <span v-if="user" class="cbc-chip">
        <span class="cbc-chip-muted">[{{ $t('component_batch_compact_card.user') }}:</span>
        {{ user }}]
      </span>
      <span v-if="host" class="cbc-chip">
        <DatabaseOutlined style="font-size: 10px" />
        <span class="cbc-chip-muted">[{{ $t('component_batch_compact_card.host') }}:</span>
        {{ host }}]
      </span>
      <span v-if="isStalled" class="cbc-stalled-badge">
        <WarningFilled />
        {{ $t('component_batch_compact_card.stalled') }}
        <template v-if="stalledAgeS">
          ({{ fmtDuration(stalledAgeS) }})
        </template>
      </span>
    </div>

    <!-- Row 2: progress count -->
    <div class="cbc-row" style="gap: 6px">
      <span class="cbc-label">{{ $t('component_batch_compact_card.progress') }}:</span>
      <span class="cbc-muted cbc-mono">{{ nDone }}/{{ nTotal }} ({{ progressPct }}%)</span>
    </div>

    <!-- Row 3: running slots. Always rendered (fixed min-height) so cards in
         the Dashboard grid align vertically whether or not a given batch has
         any running jobs at this moment. -->
    <div class="cbc-slots-block">
      <span class="cbc-label">
        {{ $t('component_batch_compact_card.running_slots', { n: runningJobs.length, m: nTotal }) }}:
      </span>
      <template v-if="visibleSlots.length">
        <div
          v-for="slot in visibleSlots"
          :key="slot.jobId"
          class="cbc-slot-row"
        >
          <span class="cbc-slot-label">[{{ slot.label }}]</span>
          <span v-if="slot.epochStr" class="cbc-slot-epoch cbc-muted">{{ slot.epochStr }}</span>
          <span v-if="slot.valLoss" class="cbc-slot-loss">
            val {{ slot.valLoss }}
            <component
              :is="trendIcon(slot.trend)"
              :style="{ color: trendColor(slot.trend), fontSize: '9px' }"
            />
          </span>
        </div>
        <span v-if="extraCount > 0" class="cbc-more-badge">
          … +{{ extraCount }} {{ $t('component_batch_compact_card.more') }}
        </span>
      </template>
      <div v-else class="cbc-slot-row cbc-muted cbc-slot-empty">
        {{ $t('component_batch_compact_card.no_running_slots') }}
      </div>
    </div>

    <!-- Row 4: start time + elapsed + ETA one-liner -->
    <div class="cbc-row cbc-time-row">
      <span v-if="cd?.batch.start_time" class="cbc-muted">
        🕐 {{ fmtTime(cd.batch.start_time) }}
      </span>
      <span v-if="elapsedSec != null" class="cbc-muted">
        | ⏱ {{ $t('component_batch_compact_card.elapsed') }} {{ fmtDuration(elapsedSec) }}
      </span>
      <span v-if="etaSec != null" class="cbc-muted">
        | {{ $t('component_batch_compact_card.eta') }} {{ fmtDuration(etaSec) }}
      </span>
    </div>

    <!-- Row 5: full telemetry chip strip -->
    <div v-if="!hasSnapshots && cd" class="cbc-row cbc-resource-row">
      <a-tag color="default" style="font-size: 10.5px; line-height: 18px; padding: 0 5px">
        {{ $t('component_batch_compact_card.waiting_for_snapshot') }}
      </a-tag>
    </div>
    <div v-else-if="hasSnapshots" class="cbc-row cbc-resource-row">
      <a-tag v-if="host" color="default" class="cbc-telemetry-tag">
        💻 {{ $t('component_batch_compact_card.chip_host') }} {{ host }}
      </a-tag>
      <a-tag v-if="pid != null" color="default" class="cbc-telemetry-tag">
        🔢 {{ $t('component_batch_compact_card.chip_pid') }} {{ pid }}
      </a-tag>
      <a-tag v-if="cpuUtil != null" :color="utilColor(cpuUtil)" class="cbc-telemetry-tag">
        🧠 {{ $t('component_batch_compact_card.chip_cpu') }} {{ Math.round(cpuUtil) }}%
      </a-tag>
      <a-tag v-if="ramMb != null" :color="ratioColor(ramMb, ramTotalMb)" class="cbc-telemetry-tag">
        🧮 {{ $t('component_batch_compact_card.chip_ram') }} {{ fmtGB(ramMb, ramTotalMb) }}
      </a-tag>
      <a-tag v-if="diskFreeMb != null" :color="diskColor(diskFreeMb, diskTotalMb)" class="cbc-telemetry-tag">
        💾 {{ $t('component_batch_compact_card.chip_disk') }} {{ fmtDiskGB(diskFreeMb) }}
      </a-tag>
      <a-tag v-if="gpuUtil != null" :color="utilColor(gpuUtil)" class="cbc-telemetry-tag">
        ⚡ {{ $t('component_batch_compact_card.chip_gpu') }} {{ Math.round(gpuUtil) }}%
      </a-tag>
      <a-tag v-if="gpuMemMb != null" :color="ratioColor(gpuMemMb, gpuMemTotalMb)" class="cbc-telemetry-tag">
        🎮 {{ $t('component_batch_compact_card.chip_vram') }} {{ fmtGB(gpuMemMb, gpuMemTotalMb) }}
      </a-tag>
      <a-tag v-if="gpuTempC != null" :color="tempColor(gpuTempC)" class="cbc-telemetry-tag">
        🌡️ {{ $t('component_batch_compact_card.chip_temp') }} {{ Math.round(gpuTempC) }}°C
      </a-tag>
      <span style="flex: 1" />
      <span
        v-if="sparklineData.length >= 2"
        class="cbc-spark-label"
      >
        {{ $t('component_batch_compact_card.progress_sparkline') }}
      </span>
      <div v-if="sparklineData.length >= 2" style="width: 60px">
        <MiniSparkline :data="sparklineData" :height="18" color="#73d13d" area />
      </div>
    </div>

    <!-- Row 6: inline alerts (failed + stalled) -->
    <div v-if="nFailed > 0 || isStalled" class="cbc-row cbc-alerts-row">
      <a-tag v-if="nFailed > 0" color="red" style="font-size: 11px; line-height: 18px; padding: 0 5px">
        <CloseCircleFilled />
        {{ $t('component_batch_compact_card.failed_count', { count: nFailed }) }}
      </a-tag>
      <a-tag v-if="isStalled" color="orange" style="font-size: 11px; line-height: 18px; padding: 0 5px">
        <WarningFilled />
        {{ $t('component_batch_compact_card.stalled_count', { minutes: stalledAgeS ? Math.round(stalledAgeS / 60) : '?' }) }}
      </a-tag>
    </div>

    <!-- Row 7: action row -->
    <div class="cbc-row cbc-actions-row">
      <a-button size="small" class="cbc-action-btn" @click="viewMatrix">
        {{ $t('component_batch_compact_card.btn_matrix') }}
      </a-button>
      <a-button size="small" class="cbc-action-btn" @click="viewJobs">
        {{ $t('component_batch_compact_card.btn_jobs') }}
      </a-button>
      <a-button size="small" class="cbc-action-btn" @click="openShare">
        <template #icon><ShareAltOutlined /></template>
        {{ $t('component_batch_compact_card.btn_share') }}
      </a-button>
      <!-- Stop button: only shown while the batch is running or already stopping -->
      <a-popconfirm
        v-if="status === 'running' || isStopping"
        :title="$t('component_batch_compact_card.stop_confirm')"
        :ok-text="$t('component_batch_compact_card.stop_confirm_ok')"
        placement="topRight"
        @confirm="handleStop"
      >
        <a-button
          size="small"
          class="cbc-action-btn"
          danger
          :loading="stopping"
          :disabled="isStopping"
        >
          <template v-if="isStopping">
            {{ $t('component_batch_compact_card.stopping_status') }}
          </template>
          <template v-else>
            {{ $t('component_batch_compact_card.btn_stop') }}
          </template>
        </a-button>
      </a-popconfirm>
    </div>

    <!-- Loading overlay (skeleton row) -->
    <div v-if="loading && !cd" class="cbc-loading">
      <a-skeleton active :paragraph="{ rows: 2 }" :title="false" />
    </div>

    <!-- Share dialog -->
    <ShareDialog
      v-if="batchId"
      v-model:open="shareOpen"
      :batch-id="batchId"
      :project="project"
    />
  </a-card>
</template>

<style scoped>
.cbc-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  font-size: 11.5px;
  line-height: 1.35;
  min-height: 18px;
}

.cbc-chip-row {
  margin-bottom: 4px;
  gap: 5px;
}

.cbc-batch-id {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 260px;
}

.cbc-chip {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.cbc-chip-muted {
  color: var(--text-tertiary);
  margin-right: 2px;
}

.cbc-stalled-badge {
  color: #fa8c16;
  font-size: 11px;
  white-space: nowrap;
}

.cbc-label {
  color: var(--text-tertiary);
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  white-space: nowrap;
}

.cbc-muted {
  color: var(--text-tertiary);
}

.cbc-mono {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-variant-numeric: tabular-nums;
}

.cbc-slots-block {
  margin: 3px 0;
  padding-left: 2px;
  /* Reserve space for label + up to MAX_VISIBLE_SLOTS=3 rows so card heights
     in the Dashboard grid stay aligned whether or not slots are present.
     Label ~14px + 3 × slot-row (13.5px inc padding) ≈ 55px. */
  min-height: 56px;
}

.cbc-slot-empty {
  font-style: italic;
  opacity: 0.6;
}

.cbc-slot-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  padding: 1px 0;
  font-family: 'SFMono-Regular', Consolas, monospace;
}

.cbc-slot-label {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.cbc-slot-epoch {
  font-size: 11px;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.cbc-slot-loss {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  color: var(--text-primary);
}

.cbc-more-badge {
  font-size: 10.5px;
  color: var(--text-tertiary);
  margin-left: 4px;
}

.cbc-time-row {
  gap: 6px;
  margin-top: 2px;
}

.cbc-resource-row {
  margin-top: 3px;
  gap: 6px;
}

.cbc-chip-resource {
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--surface-soft-strong);
  border-radius: 3px;
  padding: 0 4px;
  white-space: nowrap;
}

.cbc-telemetry-tag {
  font-size: 10.5px !important;
  line-height: 18px !important;
  padding: 0 5px !important;
  margin-right: 0 !important;
  white-space: nowrap;
}

.cbc-alerts-row {
  margin-top: 3px;
}

.cbc-actions-row {
  margin-top: 5px;
  gap: 5px;
}

.cbc-action-btn {
  font-size: 11.5px;
  height: 22px;
  padding: 0 7px;
  line-height: 20px;
}

.cbc-status-dot {
  font-size: 10px;
  line-height: 1;
}

.cbc-spark-label {
  color: var(--text-tertiary);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  margin-right: 3px;
  white-space: nowrap;
}

.cbc-loading {
  margin-top: 4px;
}
</style>
