<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useChart } from '../composables/useChart';
import { getGpuHoursByUser, type GpuHoursRow } from '../api/client';
import EmptyState from './EmptyState.vue';

/**
 * Per-user GPU-hours tile for the Dashboard (roadmap #11).
 *
 * Horizontal bar chart, top-10 users by gpu_hours for the selected
 * window. Admins see every user; non-admin viewers see just their own
 * row (backend enforces the scope — this component is view-only).
 */

const { t } = useI18n();

/** Selectable rolling window, in days. */
const WINDOW_OPTIONS = [7, 30, 90, 365] as const;
type DaysWindow = (typeof WINDOW_OPTIONS)[number];

const days = ref<DaysWindow>(30);
const loading = ref(false);
const error = ref<string | null>(null);
const rows = ref<GpuHoursRow[]>([]);

const topRows = computed<GpuHoursRow[]>(() =>
  // Backend sorts desc; we take top-10 defensively in case that changes.
  [...rows.value]
    .sort((a, b) => b.gpu_hours - a.gpu_hours)
    .slice(0, 10),
);

/** Non-empty guard for the chart — avoids echarts rendering an empty axis. */
const hasData = computed(() => topRows.value.some((r) => r.gpu_hours > 0));

const chartEl = ref<HTMLElement | null>(null);

// ECharts opt-into our dark theme via useChart(); we use a horizontal bar
// (yAxis=category, xAxis=value) so long usernames stay readable without rotation.
const option = computed(() => {
  const ordered = [...topRows.value].reverse(); // echarts draws bottom→top
  return {
    backgroundColor: 'transparent',
    grid: { left: 110, right: 20, top: 10, bottom: 30 },
    tooltip: {
      trigger: 'axis' as const,
      axisPointer: { type: 'shadow' as const },
      formatter: (params: unknown) => {
        // vue-echarts tooltip params come in as an array of series objects;
        // when trigger=axis the first element is the hovered bar.
        const arr = params as Array<{ name: string; value: number; dataIndex: number }>;
        if (!arr.length) return '';
        const row = ordered[arr[0].dataIndex];
        if (!row) return '';
        return `<b>${row.username}</b><br/>${row.gpu_hours.toFixed(2)} GPU·h<br/>${row.job_count} ${t('component_gpu_hours_tile.tooltip_jobs')}`;
      },
    },
    xAxis: {
      type: 'value',
      name: t('component_gpu_hours_tile.axis_hours'),
      nameLocation: 'middle',
      nameGap: 22,
      axisLabel: { fontSize: 10 },
    },
    yAxis: {
      type: 'category',
      data: ordered.map((r) => r.username),
      axisLabel: { fontSize: 11 },
    },
    series: [
      {
        type: 'bar',
        data: ordered.map((r) => Number(r.gpu_hours.toFixed(3))),
        itemStyle: { color: '#1677ff' },
        barMaxWidth: 18,
      },
    ],
  };
});

const { resize } = useChart(chartEl, option);

// When hasData flips (first data load or after refetch) the chart div changes
// from 0×0 to its real size. echarts caches the initial dimensions at init
// time, so we have to kick it on the next tick or the tile renders narrow
// until the first window-resize event fires.
watch(hasData, (ok) => {
  if (ok) void nextTick(resize);
});

async function fetchData(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    rows.value = await getGpuHoursByUser(days.value);
  } catch (e) {
    // eslint-disable-next-line no-console
    console.warn('[gpu-hours-tile] fetch failed', e);
    error.value = t('component_gpu_hours_tile.error_generic');
    rows.value = [];
  } finally {
    loading.value = false;
  }
}

function onDaysChange(v: unknown): void {
  const raw =
    typeof v === 'object' && v !== null && 'value' in v
      ? (v as { value: unknown }).value
      : v;
  const n = Number(raw);
  if (!Number.isFinite(n)) return;
  const snapped =
    (WINDOW_OPTIONS as readonly number[]).find((w) => w === n) ?? 30;
  days.value = snapped as DaysWindow;
}

watch(days, () => void fetchData());
onMounted(() => void fetchData());
</script>

<template>
  <a-card
    size="small"
    :body-style="{ padding: '10px 12px' }"
  >
    <template #title>
      <span style="font-size: 13px">{{ t('component_gpu_hours_tile.title') }}</span>
    </template>
    <template #extra>
      <a-select
        :value="days"
        size="small"
        style="width: 96px"
        :loading="loading"
        @change="onDaysChange"
      >
        <a-select-option
          v-for="n in WINDOW_OPTIONS"
          :key="n"
          :value="n"
        >
          {{ t('component_gpu_hours_tile.window_days', { n }) }}
        </a-select-option>
      </a-select>
    </template>

    <!-- Error surface -->
    <a-alert
      v-if="error"
      :message="error"
      type="warning"
      show-icon
      style="margin-bottom: 8px"
    />

    <!-- Empty surface: no rows or all-zero rows -->
    <EmptyState
      v-if="!loading && !hasData && !error"
      variant="empty_gpu_hours"
      :hint="t('component_gpu_hours_tile.empty_hint')"
      layout="inline"
      :icon="false"
    />

    <!-- Chart — gated on hasData so no empty-axis flash. Use v-if (not v-show)
         so the div is mounted at its full container width the first time data
         arrives; echarts then reads the correct dimensions at init. -->
    <div
      v-if="hasData"
      ref="chartEl"
      :style="{ width: '100%', height: '220px' }"
    />
  </a-card>
</template>
