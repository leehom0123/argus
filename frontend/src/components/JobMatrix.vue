<script setup lang="ts">
import { computed, ref, watch, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { TrophyOutlined, WarningFilled } from '@ant-design/icons-vue';
import type { Job } from '../types';
import { getStatusColor } from '../composables/useStatusColor';
import EmptyState from './EmptyState.vue';

const { t } = useI18n();
const props = defineProps<{
  jobs: Job[];
  /**
   * Optional experiment name shown in the redesigned section header subtitle.
   * Falls back to the first job's batch_id when omitted, then to nothing.
   */
  experimentName?: string | null;
}>();

const emit = defineEmits<{
  (e: 'pick', job: Job): void;
}>();

// ---------------------------------------------------------------------------
// localStorage — remember which metrics the user picked between sessions.
// Stored as a JSON array of metric keys; empty / corrupt values fall back to
// the auto-default below.
// ---------------------------------------------------------------------------
const STORAGE_KEY = 'argus.batch-matrix.metrics';
const MAX_METRICS = 3;

// Same hard-coded fallback list as the original labelFor() — preserves the
// "first load shows MSE / PCC / R2 / MAE in priority order" behaviour.
const DEFAULT_PRIMARY_ORDER = ['MSE', 'PCC', 'R2', 'MAE'] as const;

function loadStoredMetrics(): string[] | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const cleaned = parsed.filter((s): s is string => typeof s === 'string').slice(0, MAX_METRICS);
    return cleaned.length ? cleaned : null;
  } catch {
    return null;
  }
}

function saveStoredMetrics(keys: string[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(keys.slice(0, MAX_METRICS)));
  } catch {
    /* quota / privacy mode — silently ignore */
  }
}

// ---------------------------------------------------------------------------
// Metric direction inference. Regex picks the obvious "lower better" /
// "higher better" families; anything else defaults to lower-better and is
// flagged with a small `?` icon in the selector dropdown.
// ---------------------------------------------------------------------------
const LOWER_BETTER_RE = /loss|mse|mae|rmse|error|rmspe|smape/i;
const HIGHER_BETTER_RE = /^(?:r²|r2|pcc|scc|acc|accuracy|f1|auc|recall|precision|iou|map)$/i;

type Direction = 'lower' | 'higher';
type DirectionInference = { direction: Direction; recognized: boolean };

function inferDirection(metric: string): DirectionInference {
  if (LOWER_BETTER_RE.test(metric)) return { direction: 'lower', recognized: true };
  if (HIGHER_BETTER_RE.test(metric)) return { direction: 'higher', recognized: true };
  return { direction: 'lower', recognized: false };
}

// User overrides for unrecognized metrics. Maps metric name → forced direction.
const directionOverrides = ref<Record<string, Direction>>({});

function effectiveDirection(metric: string): Direction {
  const override = directionOverrides.value[metric];
  if (override) return override;
  return inferDirection(metric).direction;
}

// ---------------------------------------------------------------------------
// Existing perf-popover helpers (unchanged from prior version) — kept so the
// hover popover still surfaces avg_batch_time_ms / gpu_memory_peak_mb /
// n_params for #21. Only the cell render/colour code below is rewritten.
// ---------------------------------------------------------------------------
function readNumber(job: Job, key: string): number | null {
  const v = (job as unknown as Record<string, unknown>)[key];
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function fmtAvgBatchMs(job: Job): string {
  const v = readNumber(job, 'avg_batch_time_ms');
  return v == null ? '' : `${Math.round(v)} ms`;
}

function fmtGpuPeakMb(job: Job): string {
  const v = readNumber(job, 'gpu_memory_peak_mb');
  if (v == null) return '';
  return v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${Math.round(v)} MB`;
}

function fmtNParams(job: Job): string {
  const v = readNumber(job, 'n_params');
  if (v == null) return '';
  if (v >= 1_000_000_000) return `${(v / 1e9).toFixed(1)}B`;
  if (v >= 1_000_000) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1e3).toFixed(1)}K`;
  return String(v);
}

function jobMetricsEntries(job: Job): Array<[string, number]> {
  const m = job.metrics ?? {};
  return Object.entries(m)
    .filter(([, v]) => typeof v === 'number' && Number.isFinite(v as number))
    .map(([k, v]) => [k, v as number]);
}

// ---------------------------------------------------------------------------
// Axes: unique models (rows) and datasets (cols), preserving first-seen
// order so the matrix layout stays stable as new jobs trickle in.
// ---------------------------------------------------------------------------
const models = computed(() => {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const j of props.jobs) {
    const m = j.model ?? '—';
    if (!seen.has(m)) {
      seen.add(m);
      out.push(m);
    }
  }
  return out;
});

const datasets = computed(() => {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const j of props.jobs) {
    const d = j.dataset ?? '—';
    if (!seen.has(d)) {
      seen.add(d);
      out.push(d);
    }
  }
  return out;
});

// (model, dataset) → Job lookup
const matrix = computed(() => {
  const map = new Map<string, Job>();
  for (const j of props.jobs) {
    const key = `${j.model ?? '—'}||${j.dataset ?? '—'}`;
    map.set(key, j);
  }
  return map;
});

// ---------------------------------------------------------------------------
// Available metric keys: union of every job.metrics's keys. Sorted with the
// known forecast metrics first (MSE/MAE/R2/PCC/SCC/RMSE) for selector
// usability, then anything extra alphabetically.
// ---------------------------------------------------------------------------
const KNOWN_METRIC_PRIORITY = ['MSE', 'MAE', 'RMSE', 'R2', 'PCC', 'SCC'];

const availableMetrics = computed<string[]>(() => {
  const seen = new Set<string>();
  for (const j of props.jobs) {
    if (!j.metrics) continue;
    for (const [k, v] of Object.entries(j.metrics)) {
      if (typeof v === 'number' && Number.isFinite(v)) seen.add(k);
    }
  }
  const known = KNOWN_METRIC_PRIORITY.filter((k) => seen.has(k));
  const rest = [...seen].filter((k) => !KNOWN_METRIC_PRIORITY.includes(k)).sort();
  return [...known, ...rest];
});

// ---------------------------------------------------------------------------
// Selected metrics — what the cells actually render.
// On first mount: pick the highest-priority single metric so cells look
// identical to the pre-multi-metric version.
// On subsequent mounts: localStorage value (if still valid against jobs).
// ---------------------------------------------------------------------------
const selectedMetrics = ref<string[]>([]);
const showMaxWarning = ref(false);

function pickInitialMetrics(): string[] {
  const stored = loadStoredMetrics();
  if (stored && stored.length) {
    // Filter to ones that still exist in the current job set so we never
    // render selected-but-missing metrics. If everything was filtered out,
    // fall through to the auto-default.
    const filtered = stored.filter((k) => availableMetrics.value.includes(k));
    if (filtered.length) return filtered;
  }
  // Default: first hit from the priority list, else first available, else nothing.
  for (const k of DEFAULT_PRIMARY_ORDER) {
    if (availableMetrics.value.includes(k)) return [k];
  }
  return availableMetrics.value.length ? [availableMetrics.value[0]] : [];
}

onMounted(() => {
  selectedMetrics.value = pickInitialMetrics();
});

// If a parent reload swaps in a different job set with no overlap, reset.
watch(availableMetrics, (next) => {
  if (!selectedMetrics.value.length && next.length) {
    selectedMetrics.value = pickInitialMetrics();
    return;
  }
  const stillValid = selectedMetrics.value.filter((k) => next.includes(k));
  if (stillValid.length !== selectedMetrics.value.length) {
    selectedMetrics.value = stillValid.length ? stillValid : pickInitialMetrics();
  }
});

watch(selectedMetrics, (next) => {
  saveStoredMetrics(next);
});

function onMetricSelectChange(raw: unknown): void {
  // a-select emits SelectValue (string | string[] | undefined) for
  // multi-mode, but ``mode="multiple"`` always hands us an array. We
  // normalise defensively so the test stub's plain string[] also lands
  // here without TypeScript complaining.
  const next: string[] = Array.isArray(raw)
    ? (raw.filter((s) => typeof s === 'string') as string[])
    : [];
  if (next.length > MAX_METRICS) {
    // Truncate and surface the warning. Vue + a-select pass us the *new*
    // array including the over-limit pick, so we crop in place.
    selectedMetrics.value = next.slice(0, MAX_METRICS);
    showMaxWarning.value = true;
    setTimeout(() => {
      showMaxWarning.value = false;
    }, 2400);
    return;
  }
  selectedMetrics.value = next;
}

const primaryMetric = computed<string | null>(
  () => selectedMetrics.value[0] ?? null,
);

// ---------------------------------------------------------------------------
// Best / worst computation — #126 redesign.
//
// Earlier the matrix tinted every cell with its status colour and added
// per-column / per-row rank classes (col-best, col-good, col-poor,
// col-worst, row-best, row-worst). Operators told us "整片绿色读不出
// 信息" and "突出最优最差就够了". So now we:
//
//   1. Render every cell with a white background + thin gray border by
//      default (see ``.matrix-cell`` in <style>). Status is conveyed by
//      a small dot in the top-right corner — same colour as the rest of
//      the unified 5-bucket scheme (#125).
//   2. Compute exactly one global best and one global worst across the
//      entire matrix, on the primary metric. Best gets a thicker green
//      border + trophy icon; worst gets a thicker red border + warning
//      icon. Ties (multiple cells share the best/worst value) are skipped
//      — celebrating any one of them would be misleading.
//   3. Only ``status === 'done'`` cells with a finite metric value are
//      eligible. We deliberately skip stalled / failed / running cells:
//      "best running cell" is meaningless when the run isn't finished,
//      and "worst failed cell" would flag a crash as a poor result.
//      ``is_idle_flagged`` (canonical stalled bucket per useStatusColor)
//      also disqualifies the cell from best/worst.
// ---------------------------------------------------------------------------

/**
 * Eligibility filter for best/worst designation. Returns false for any
 * cell that is not a clean ``done`` job with a finite primary metric.
 */
function isEligibleForBestWorst(j: Job | undefined, metric: string): boolean {
  if (!j) return false;
  if ((j.status ?? '').toLowerCase() !== 'done') return false;
  if (j.is_idle_flagged) return false;
  const v = j.metrics?.[metric];
  return typeof v === 'number' && Number.isFinite(v);
}

interface BestWorstKey {
  model: string;
  dataset: string;
}

interface BestWorstResult {
  bestKey: string | null;
  worstKey: string | null;
}

const bestWorstCells = computed<BestWorstResult>(() => {
  const metric = primaryMetric.value;
  if (!metric) return { bestKey: null, worstKey: null };
  const dir = effectiveDirection(metric);

  // Collect (model, dataset, value) for every eligible cell.
  const eligible: Array<{ key: BestWorstKey; v: number }> = [];
  for (const mdl of models.value) {
    for (const ds of datasets.value) {
      const j = matrix.value.get(`${mdl}||${ds}`);
      if (!isEligibleForBestWorst(j, metric)) continue;
      // The eligibility check guarantees finiteness, so the cast is safe.
      eligible.push({ key: { model: mdl, dataset: ds }, v: j!.metrics![metric] as number });
    }
  }
  if (eligible.length < 2) {
    // With 0 or 1 eligible cells there's no comparison to make, so we
    // skip the highlight entirely. (Single-cell batches stay neutral.)
    return { bestKey: null, worstKey: null };
  }

  // Compute extremum values per direction.
  const values = eligible.map((e) => e.v);
  const bestVal = dir === 'lower' ? Math.min(...values) : Math.max(...values);
  const worstVal = dir === 'lower' ? Math.max(...values) : Math.min(...values);

  // Tie-handling: when every eligible cell shares the same value (degenerate
  // "all equal" batch), neither a best nor a worst is defined. Likewise, if
  // multiple cells tie for best (or worst) we don't pick an arbitrary winner.
  if (bestVal === worstVal) return { bestKey: null, worstKey: null };

  const bestMatches = eligible.filter((e) => e.v === bestVal);
  const worstMatches = eligible.filter((e) => e.v === worstVal);

  return {
    bestKey: bestMatches.length === 1 ? `${bestMatches[0].key.model}||${bestMatches[0].key.dataset}` : null,
    worstKey: worstMatches.length === 1 ? `${worstMatches[0].key.model}||${worstMatches[0].key.dataset}` : null,
  };
});

function isBestCell(model: string, dataset: string): boolean {
  return bestWorstCells.value.bestKey === `${model}||${dataset}`;
}

function isWorstCell(model: string, dataset: string): boolean {
  return bestWorstCells.value.worstKey === `${model}||${dataset}`;
}

// ---------------------------------------------------------------------------
// Cell helpers
// ---------------------------------------------------------------------------

/**
 * Per-cell status dot colour. Returns ``null`` when the cell needs no dot
 * (i.e. ``done`` without idle-flag — the default white state already says
 * "this run finished cleanly", no decoration needed).
 */
function statusDotColor(j?: Job): string | null {
  if (!j) return null;
  const tokens = getStatusColor('job', j.status ?? '', { isIdleFlagged: !!j.is_idle_flagged });
  // Skip the dot for clean ``done`` cells — keeps the matrix calm.
  if (tokens.bucket === 'done' && !j.is_idle_flagged) return null;
  return tokens.border;
}

/**
 * Whether the dot should pulse — running cells get a soft pulse so an
 * in-flight sweep is visually obvious without dominating the grid.
 */
function statusDotPulses(j?: Job): boolean {
  if (!j) return false;
  return getStatusColor('job', j.status ?? '', { isIdleFlagged: !!j.is_idle_flagged }).bucket === 'running';
}

/**
 * Aria-label for the status dot. Falls back to the unified label so
 * screen readers hear "Status: Running" / "Status: Failed" etc.
 */
function statusDotAria(j?: Job): string {
  if (!j) return '';
  return getStatusColor('job', j.status ?? '', { isIdleFlagged: !!j.is_idle_flagged }).aria;
}

function fmtMetricValue(v: number): string {
  // Compact scientific for very large/small magnitudes, else 3 decimals.
  if (!Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  if (abs !== 0 && (abs >= 1e4 || abs < 1e-2)) return v.toExponential(2);
  return v.toFixed(3);
}

function cellMetricValues(j: Job | undefined): Array<{ key: string; value: number | null }> {
  const out: Array<{ key: string; value: number | null }> = [];
  if (!j) return out;
  const m = j.metrics ?? {};
  for (const k of selectedMetrics.value) {
    const v = m[k];
    out.push({ key: k, value: typeof v === 'number' && Number.isFinite(v) ? v : null });
  }
  return out;
}

function fallbackLabel(j: Job): string {
  // When no metric is selected (e.g. jobs report nothing yet), show the
  // first 4 chars of the status — same as the original component.
  return (j.status ?? '').slice(0, 4);
}

// ---------------------------------------------------------------------------
// Best-job chip in the section subtitle: pick the global best across the
// matrix on the primary metric. When metric is unknown / no jobs report it,
// chip is hidden.
// ---------------------------------------------------------------------------
const bestJob = computed<{ job: Job; value: number; metric: string } | null>(() => {
  const metric = primaryMetric.value;
  if (!metric) return null;
  const dir = effectiveDirection(metric);
  let best: { job: Job; value: number } | null = null;
  for (const j of props.jobs) {
    const v = j.metrics?.[metric];
    if (typeof v !== 'number' || !Number.isFinite(v)) continue;
    if (best === null) {
      best = { job: j, value: v };
      continue;
    }
    const isBetter = dir === 'lower' ? v < best.value : v > best.value;
    if (isBetter) best = { job: j, value: v };
  }
  return best ? { ...best, metric } : null;
});

const subtitleText = computed<string>(() => {
  const parts: string[] = [];
  const exp = props.experimentName;
  if (exp) parts.push(exp);
  parts.push(t('matrix.job_count', { n: props.jobs.length }));
  return parts.join(' · ');
});
</script>

<template>
  <EmptyState v-if="!jobs.length" variant="empty_jobs" layout="inline" />
  <div v-else class="job-matrix">
    <!-- ===================================================================
         Section header — title + metric selector live on the same row, with
         experiment subtitle underneath. Replaces the old plain-text label.
         =================================================================== -->
    <header class="matrix-header">
      <div class="matrix-header-row">
        <div class="matrix-title">
          <span class="matrix-title-icon" aria-hidden="true">⚡</span>
          {{ t('matrix.title') }}
        </div>

        <a-select
          mode="multiple"
          :value="selectedMetrics"
          :max-tag-count="3"
          :options="
            availableMetrics.map((k) => ({
              label: inferDirection(k).recognized
                ? k
                : `${k}  ?`,
              value: k,
              title: inferDirection(k).recognized
                ? undefined
                : t('matrix.direction_question'),
            }))
          "
          :placeholder="t('matrix.metrics_label')"
          style="min-width: 240px; max-width: 360px"
          size="small"
          class="metric-select"
          data-testid="metric-select"
          @update:value="onMetricSelectChange"
        />

        <span v-if="showMaxWarning" class="max-warning" data-testid="max-warning">
          {{ t('matrix.metric_max_warning') }}
        </span>

        <span class="header-spacer" />
      </div>

      <div v-if="subtitleText || bestJob" class="matrix-subtitle">
        <span v-if="subtitleText">{{ subtitleText }}</span>
        <span v-if="bestJob" class="best-chip" data-testid="best-chip">
          <TrophyOutlined />
          {{ t('matrix.best_chip') }}
          <strong>{{ bestJob.job.model ?? '—' }}</strong>
          <span class="best-chip-value">{{ fmtMetricValue(bestJob.value) }}</span>
          <span class="best-chip-metric">({{ bestJob.metric }})</span>
        </span>
      </div>
    </header>

    <!-- ===================================================================
         Matrix table — kept as a CSS grid (not a-table) because the fixed
         left column + tight cell sizing is hard to replicate with a-table
         without losing the scroll behaviour. Cells now inject rank classes
         driven by the primary metric.
         =================================================================== -->
    <div
      :style="{
        display: 'grid',
        gridTemplateColumns: `160px repeat(${datasets.length}, minmax(96px, 1fr))`,
        gap: '4px',
        alignItems: 'stretch',
        overflowX: 'auto',
      }"
    >
      <!-- header row -->
      <div />
      <div
        v-for="d in datasets"
        :key="`h-${d}`"
        class="muted"
        style="font-size: 11px; text-align: center; padding: 4px 2px; word-break: break-all"
      >
        {{ d }}
      </div>

      <!-- body rows -->
      <template v-for="m in models" :key="`r-${m}`">
        <div
          class="muted"
          style="font-size: 12px; padding: 4px 8px; display: flex; align-items: center; word-break: break-all"
        >
          {{ m }}
        </div>
        <a-popover
          v-for="d in datasets"
          :key="`c-${m}-${d}`"
          :mouse-enter-delay="0.2"
          :mouse-leave-delay="0.1"
          placement="top"
        >
          <template #title>
            <span style="font-size: 12px">
              {{ matrix.get(`${m}||${d}`) ? t('component_job_matrix_popover.title') : t('component_job_matrix.no_job') }}
            </span>
          </template>
          <template #content>
            <div
              v-if="matrix.get(`${m}||${d}`)"
              style="font-size: 12px; line-height: 1.7; min-width: 220px; max-width: 320px"
            >
              <div>
                <span class="muted">{{ t('component_job_matrix_popover.label_model') }}:</span>
                {{ matrix.get(`${m}||${d}`)?.model ?? '—' }}
              </div>
              <div>
                <span class="muted">{{ t('component_job_matrix_popover.label_dataset') }}:</span>
                {{ matrix.get(`${m}||${d}`)?.dataset ?? '—' }}
              </div>
              <div>
                <span class="muted">{{ t('component_job_matrix_popover.label_status') }}:</span>
                {{ matrix.get(`${m}||${d}`)?.status ?? '—' }}
              </div>
              <div v-if="matrix.get(`${m}||${d}`)?.elapsed_s">
                <span class="muted">{{ t('component_job_matrix_popover.label_elapsed') }}:</span>
                {{ matrix.get(`${m}||${d}`)?.elapsed_s }}s
              </div>
              <div v-if="fmtAvgBatchMs(matrix.get(`${m}||${d}`)!)">
                <span class="muted">{{ t('component_job_matrix_popover.label_avg_batch_time') }}:</span>
                {{ fmtAvgBatchMs(matrix.get(`${m}||${d}`)!) }}
              </div>
              <div v-if="fmtGpuPeakMb(matrix.get(`${m}||${d}`)!)">
                <span class="muted">{{ t('component_job_matrix_popover.label_gpu_peak') }}:</span>
                {{ fmtGpuPeakMb(matrix.get(`${m}||${d}`)!) }}
              </div>
              <div v-if="fmtNParams(matrix.get(`${m}||${d}`)!)">
                <span class="muted">{{ t('component_job_matrix_popover.label_n_params') }}:</span>
                {{ fmtNParams(matrix.get(`${m}||${d}`)!) }}
              </div>
              <div v-if="matrix.get(`${m}||${d}`)?.is_idle_flagged" style="color: #faad14">
                <span class="muted">⚠</span>
                {{ t('component_job_badge.idle_tooltip_generic') }}
              </div>

              <!-- Hover lists EVERY metric (not just the 1-3 selected) — the
                   selection is a display filter, the popover is the full
                   debugging view. -->
              <div
                v-if="jobMetricsEntries(matrix.get(`${m}||${d}`)!).length"
                style="margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--border-soft)"
              >
                <div class="muted" style="margin-bottom: 2px">
                  {{ t('component_job_matrix_popover.metrics_title') }}
                </div>
                <table
                  class="popover-metrics-table"
                  data-testid="popover-metrics-table"
                  style="font-family: monospace; font-size: 11px; border-collapse: collapse"
                >
                  <tr
                    v-for="[k, v] in jobMetricsEntries(matrix.get(`${m}||${d}`)!)"
                    :key="k"
                  >
                    <td style="padding: 0 8px 0 0; opacity: 0.8">{{ k }}</td>
                    <td style="text-align: right">{{ v.toFixed(4) }}</td>
                  </tr>
                </table>
              </div>
              <div
                v-else
                class="muted"
                style="margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--border-soft); font-size: 11px"
              >
                {{ t('component_job_matrix_popover.no_metrics') }}
              </div>
            </div>
            <div v-else class="muted" style="font-size: 12px; min-width: 160px">
              {{ t('component_job_matrix.no_job') }}
            </div>
          </template>

          <div
            class="matrix-cell"
            :class="{
              'cell-best': isBestCell(m, d),
              'cell-worst': isWorstCell(m, d),
              'cell-empty': !matrix.get(`${m}||${d}`),
            }"
            @click="() => matrix.get(`${m}||${d}`) && emit('pick', matrix.get(`${m}||${d}`)!)"
          >
            <!-- Best / worst overlay icons. Mutually exclusive (a cell is
                 either the single best, the single worst, or neither). -->
            <span
              v-if="isBestCell(m, d)"
              class="cell-flag cell-flag-best"
              :title="t('matrix.best')"
              :aria-label="t('matrix.best')"
              data-testid="cell-flag-best"
            >
              <TrophyOutlined />
            </span>
            <span
              v-else-if="isWorstCell(m, d)"
              class="cell-flag cell-flag-worst"
              :title="t('matrix.worst')"
              :aria-label="t('matrix.worst')"
              data-testid="cell-flag-worst"
            >
              <WarningFilled />
            </span>

            <template v-if="matrix.get(`${m}||${d}`) && cellMetricValues(matrix.get(`${m}||${d}`)).some((e) => e.value != null)">
              <span
                v-for="(entry, idx) in cellMetricValues(matrix.get(`${m}||${d}`))"
                :key="entry.key"
                :class="['metric-piece', idx === 0 ? 'metric-primary' : 'metric-secondary']"
              >
                <span v-if="idx > 0" class="sep">/</span>
                <span class="metric-value">{{ entry.value == null ? '—' : fmtMetricValue(entry.value) }}</span>
              </span>
            </template>
            <template v-else-if="matrix.get(`${m}||${d}`)">
              {{ fallbackLabel(matrix.get(`${m}||${d}`)!) }}
            </template>

            <!-- Status dot — top-right, sized 8px, picks colour from the
                 unified scheme (#125). Hidden for clean ``done`` cells so
                 the matrix isn't a wall of green. -->
            <span
              v-if="statusDotColor(matrix.get(`${m}||${d}`))"
              class="status-dot"
              :class="{ 'status-dot-pulse': statusDotPulses(matrix.get(`${m}||${d}`)) }"
              :style="{ background: statusDotColor(matrix.get(`${m}||${d}`)) || undefined }"
              :aria-label="statusDotAria(matrix.get(`${m}||${d}`))"
              data-testid="status-dot"
            />
          </div>
        </a-popover>
      </template>
    </div>
  </div>
</template>

<style scoped>
.job-matrix {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* Section header — bigger, denser, with metric controls inline. */
.matrix-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 4px 0 8px;
  border-bottom: 1px solid var(--matrix-header-border);
}
.matrix-header-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.matrix-title {
  font-size: 22px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  line-height: 1.3;
}
.matrix-title-icon {
  font-size: 18px;
}
.metric-select {
  /* relies on a-select's own font sizing */
}
.max-warning {
  color: #d4380d;
  font-size: 12px;
  font-weight: 500;
}
.header-spacer {
  flex: 1;
}
.matrix-subtitle {
  font-size: 13px;
  color: var(--matrix-subtitle-color);
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.best-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--matrix-best-chip-bg);
  color: var(--matrix-best-chip-color);
  font-size: 12px;
}
.best-chip strong {
  font-weight: 600;
  margin: 0 2px;
}
.best-chip-value {
  font-family: monospace;
}
.best-chip-metric {
  opacity: 0.65;
}

/* Cell rendering — colours from CSS variables defined in styles.css so
   dark mode flips automatically without :global() hacks. */
.matrix-cell {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 2px;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  font-family: monospace;
  color: var(--matrix-cell-color);
  background: var(--matrix-cell-bg);
  border: 1px solid var(--matrix-cell-border);
  min-height: 28px;
  line-height: 1.2;
}
.matrix-cell:hover {
  border-color: var(--text-secondary);
}
.matrix-cell.cell-empty {
  background: var(--matrix-cell-empty-bg);
  cursor: default;
  color: var(--matrix-cell-empty-color);
}
.metric-piece {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.metric-primary {
  font-weight: 500;
}
.metric-secondary {
  color: var(--matrix-secondary-color);
  font-size: 0.92em;
}
.metric-piece .sep {
  opacity: 0.5;
  margin: 0 1px;
}

/* Best cell — green 2px border + trophy icon top-right. Border colour
   matches the running bucket from #125 (#52c41a) so the visual language
   stays consistent. */
.matrix-cell.cell-best {
  border: 2px solid #52c41a;
  padding: 5px 7px; /* compensate for the +1px border so layout doesn't shift */
  box-shadow: 0 0 0 1px rgba(82, 196, 26, 0.18);
}

/* Worst cell — red 2px border + warning icon. Uses the failed bucket
   colour (#ff4d4f). */
.matrix-cell.cell-worst {
  border: 2px solid #ff4d4f;
  padding: 5px 7px;
  box-shadow: 0 0 0 1px rgba(255, 77, 79, 0.18);
}

.cell-flag {
  position: absolute;
  top: 2px;
  left: 3px;
  font-size: 11px;
  line-height: 1;
  pointer-events: none;
}
.cell-flag-best {
  color: #52c41a;
}
.cell-flag-worst {
  color: #ff4d4f;
}

/* Status dot — top-right corner, 8px diameter, no decoration on clean
   ``done`` cells (statusDotColor() returns null then). Pulses for the
   running bucket so an in-flight sweep is visually obvious. */
.status-dot {
  position: absolute;
  top: 3px;
  right: 3px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  box-shadow: 0 0 0 1.5px rgba(255, 255, 255, 0.85);
  pointer-events: none;
}
.status-dot-pulse {
  animation: matrix-cell-dot-pulse 1.6s ease-in-out infinite;
}
@keyframes matrix-cell-dot-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.55;
    transform: scale(1.25);
  }
}

/* Dark-mode status dot ring — contrasts against the dark cell background.
   All other dark-mode overrides are handled via CSS variables in styles.css. */
:global(html.dark) .status-dot {
  box-shadow: 0 0 0 1.5px rgba(0, 0, 0, 0.55);
}
</style>
