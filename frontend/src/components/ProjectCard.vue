<script setup lang="ts">
import { computed } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import {
  FieldTimeOutlined,
  AimOutlined,
  ExperimentOutlined,
  WarningFilled,
  TrophyOutlined,
  ThunderboltOutlined,
  TeamOutlined,
} from '@ant-design/icons-vue';
import type { ProjectSummary } from '../types';
import { fmtRelative, fmtDuration } from '../utils/format';
import { statusBorderColor } from '../utils/status';
import { getStatusColor } from '../composables/useStatusColor';
import StarButton from './StarButton.vue';
import MiniSparkline from './MiniSparkline.vue';

const { t } = useI18n();
const props = defineProps<{
  project: ProjectSummary;
  /** When true, hide write actions (star button) — demo / public viewers. */
  readOnly?: boolean;
}>();
const router = useRouter();

const running = computed(() => props.project.running_batches ?? 0);
// Backend uses `n_batches`; legacy / mocked rows still ship `total_batches`.
const total = computed(
  () => props.project.total_batches ?? props.project.n_batches ?? 0,
);
const failedJobs = computed(() => props.project.jobs_failed ?? 0);
const doneJobs = computed(() => props.project.jobs_done ?? 0);

function open() {
  // In read-only mode we stay inside the demo route-tree so the banner
  // and permission prop propagate with the navigation.
  const prefix = props.readOnly ? '/demo/projects/' : '/projects/';
  router.push(prefix + encodeURIComponent(props.project.project));
}

const bestLabel = computed(() => {
  const key = props.project.best_metric_key;
  const v = props.project.best_metric;
  if (v === null || v === undefined || !Number.isFinite(v)) return null;
  return `${key ?? 'metric'} ${v.toFixed(4)}`;
});

/**
 * Project-level status for the left border. ProjectSummary doesn't carry
 * the batch list, so we infer from counters: any running batch → running;
 * else any failed job → failed; else done. If nothing's happened we
 * fall through to transparent.
 */
const derivedStatus = computed<string>(() => {
  const p = props.project;
  if ((p.running_batches ?? 0) > 0) return 'running';
  if ((p.jobs_failed ?? 0) > 0) return 'failed';
  if (total.value > 0) return 'done';
  return '';
});

/**
 * Status colour tokens for the unified 5-colour scheme (#125). Drives the
 * ``aria-label`` on the outer card so screen readers announce the
 * project's rolled-up health alongside the visual border colour.
 */
const statusTokens = computed(() =>
  getStatusColor('project', derivedStatus.value, {
    runningBatches: props.project.running_batches ?? 0,
    jobsFailed: props.project.jobs_failed ?? 0,
    totalBatches: total.value,
  }),
);

// ── v0.1.3 density rows ─────────────────────────────────────────────────────

/** Top-3 models, falling back to an empty list so v-if hides the row. */
const topModels = computed(() => props.project.top_models ?? []);

/** Failure rate as an already-rounded percentage string (or null). */
const failureRatePct = computed(() => {
  const fr = props.project.failure_rate;
  if (fr === null || fr === undefined || !Number.isFinite(fr)) return null;
  return Math.round(fr * 100);
});

/** GPU hours as a 1-decimal number string (or null when unset / zero). */
const gpuHoursLabel = computed(() => {
  const g = props.project.gpu_hours;
  if (g === null || g === undefined || !Number.isFinite(g)) return null;
  if (g <= 0) return null;
  return g.toFixed(1);
});

/** Sparkline data: only render when the array has the full 7 entries. */
const batchVolume7d = computed(() => {
  const arr = props.project.batch_volume_7d;
  if (!Array.isArray(arr) || arr.length === 0) return null;
  return arr;
});

const totalBatchesLast7d = computed(() => {
  const arr = batchVolume7d.value;
  if (!arr) return 0;
  return arr.reduce((sum, n) => sum + (n ?? 0), 0);
});

/** Owner / collaborator chip — first owner inline, "+N" when there are more. */
const ownersChip = computed(() => {
  const owners = props.project.owners ?? [];
  if (!owners.length) return null;
  const first = owners[0];
  const extra = owners.length - 1;
  return extra > 0 ? `${first} +${extra}` : first;
});

const etaSeconds = computed(
  () => props.project.eta ?? props.project.eta_seconds ?? null,
);
</script>

<template>
  <a-card
    size="small"
    hoverable
    :bodyStyle="{ padding: '12px 14px' }"
    :style="{
      cursor: 'pointer',
      height: '100%',
      borderLeft: `4px solid ${statusBorderColor(derivedStatus)}`,
    }"
    :aria-label="statusTokens.aria"
    :data-status-bucket="statusTokens.bucket"
    @click="open"
  >
    <!-- Brand row: project name + (optional) owner chip -->
    <div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 8px">
      <div style="min-width: 0; flex: 1">
        <div
          style="font-weight: 600; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis"
        >
          {{ project.project }}
          <span
            v-if="ownersChip"
            class="muted"
            style="font-size: 11px; font-weight: 400; margin-left: 6px"
          >
            <TeamOutlined /> {{ ownersChip }}
          </span>
        </div>
        <div class="muted" style="font-size: 11px; margin-top: 1px">
          <span v-if="project.last_event_at">
            <FieldTimeOutlined />
            {{ $t('component_project_card.last_event', { time: fmtRelative(project.last_event_at) }) }}
          </span>
          <span v-else>{{ $t('component_project_card.no_events') }}</span>
        </div>
      </div>

      <!-- Star button: stops propagation itself. Hidden in read-only mode. -->
      <StarButton
        v-if="!readOnly"
        target-type="project"
        :target-id="project.project"
        icon-only
      />
    </div>

    <!-- Health strip: running / total / failed counters (existing). -->
    <div style="display: flex; gap: 16px; margin-top: 10px; flex-wrap: wrap">
      <div>
        <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ $t('component_project_card.label_running') }}</div>
        <div
          :style="{
            fontSize: '18px',
            fontWeight: 600,
            color: running > 0 ? '#52c41a' : '#888',
          }"
        >
          {{ running }}
        </div>
      </div>
      <div>
        <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ $t('component_project_card.label_batches') }}</div>
        <div style="font-size: 18px; font-weight: 600">{{ total }}</div>
      </div>
      <div v-if="failedJobs > 0">
        <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ $t('component_project_card.label_failed_jobs') }}</div>
        <div style="font-size: 18px; font-weight: 600; color: #ff4d4f">
          {{ failedJobs }}
        </div>
      </div>
      <div v-if="failureRatePct !== null">
        <div class="muted" style="font-size: 10.5px; text-transform: uppercase">{{ $t('component_project_card.fail_rate') }}</div>
        <div
          :style="{
            fontSize: '18px',
            fontWeight: 600,
            color: failureRatePct >= 20 ? '#ff7875' : '#aaa',
          }"
        >
          {{ failureRatePct }}%
        </div>
      </div>
    </div>

    <!-- Winner row: top-1 model × dataset + headline metric. -->
    <div
      v-if="topModels.length > 0"
      class="muted"
      style="font-size: 11.5px; margin-top: 8px; display: flex; gap: 6px; align-items: center"
    >
      <TrophyOutlined style="color: #faad14" />
      <span>
        {{ $t('component_project_card.top_models') }}:
        <span style="color: var(--text-primary); font-weight: 500">
          {{ topModels[0].model ?? '—' }} × {{ topModels[0].dataset ?? '—' }}
        </span>
        <span style="margin-left: 4px">
          {{ topModels[0].metric_name }} {{ topModels[0].metric_value.toFixed(4) }}
        </span>
      </span>
    </div>

    <!-- Trend row: 7-day sparkline + total + GPU-hours. -->
    <div
      v-if="batchVolume7d || gpuHoursLabel"
      style="font-size: 11px; margin-top: 6px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap"
    >
      <div v-if="batchVolume7d" style="width: 60px; height: 18px">
        <MiniSparkline :data="batchVolume7d" :height="18" color="#52c41a" area />
      </div>
      <span v-if="batchVolume7d" class="muted">
        {{ $t('component_project_card.batches_per_week', { n: totalBatchesLast7d }) }}
      </span>
      <span v-if="gpuHoursLabel" class="muted">
        <ThunderboltOutlined /> {{ gpuHoursLabel }} {{ $t('component_project_card.gpu_hrs_unit') }}
      </span>
    </div>

    <!-- Freshness row: existing best-metric + ETA + failed (kept). -->
    <div
      class="muted"
      style="
        font-size: 11px;
        margin-top: 8px;
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        align-items: center;
      "
    >
      <span v-if="bestLabel">
        <AimOutlined /> {{ $t('component_project_card.best', { label: bestLabel }) }}
      </span>
      <span v-if="etaSeconds">
        <ExperimentOutlined /> {{ $t('component_project_card.eta', { duration: fmtDuration(etaSeconds) }) }}
      </span>
      <span v-if="running > 0 && !etaSeconds">
        <ExperimentOutlined /> {{ $t('component_project_card.running_ellipsis') }}
      </span>
      <span v-if="failedJobs > 0" style="color: #ff7875">
        <WarningFilled /> {{ $t('component_project_card.failed_count', { count: failedJobs }) }}
      </span>
    </div>
  </a-card>
</template>
