<script setup lang="ts">
/**
 * StackedResourceChart.vue
 *
 * Displays how a host's GPU/RAM/CPU is split among concurrent batches
 * over a configurable rolling window. Uses an ECharts stacked-area chart;
 * an extra dashed line shows the host-level `total` for comparison.
 *
 * Props:
 *   host        — host name to query
 *   metric      — one of gpu_mem_mb | gpu_util_pct | cpu_util_pct | ram_mb
 *   windowHours — look-back window in hours (default: 1)
 *
 * Wiring note:
 *   This component is NOT yet mounted in HostDetail.vue — a parallel UI
 *   agent (a03972403edae6bbd) is editing that page and we must avoid
 *   conflicts. Wire it into HostDetail.vue's "Stacked breakdown" block in
 *   a follow-up PR after the UI host agent commits.
 */
import { ref, computed, watch, onMounted } from 'vue';
import dayjs from 'dayjs';
import { useI18n } from 'vue-i18n';
import { useChart } from '../composables/useChart';
import { getHostTimeseries, type HostTimeseriesBucket } from '../api/client';
import { cached } from '../composables/useCache';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

const props = withDefaults(
  defineProps<{
    host: string;
    metric?: 'gpu_mem_mb' | 'gpu_util_pct' | 'cpu_util_pct' | 'ram_mb';
    windowHours?: number;
    height?: number;
    bucketSeconds?: number;
  }>(),
  {
    metric: 'gpu_mem_mb',
    windowHours: 1,
    height: 320,
    bucketSeconds: 60,
  },
);

// ---------------------------------------------------------------------------
// i18n
// ---------------------------------------------------------------------------

const { t } = useI18n();

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const buckets = ref<HostTimeseriesBucket[]>([]);
const capacity = ref<number | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);

// ---------------------------------------------------------------------------
// Data fetch
// ---------------------------------------------------------------------------

async function fetchData(): Promise<void> {
  if (!props.host) return;
  loading.value = true;
  error.value = null;
  try {
    // 30s TTL + in-flight dedup: multiple charts on the same page hitting
    // the same (host, metric, window) reuse one HTTP call, and navigating
    // back within 30s paints from cache.
    const cacheKey = `host-ts:${props.host}:${props.metric}:${props.windowHours}h:${props.bucketSeconds}s`;
    const result = await cached(
      cacheKey,
      () => getHostTimeseries(props.host, {
        metric: props.metric,
        since: `now-${props.windowHours}h`,
        bucket_seconds: props.bucketSeconds,
      }),
      10_000,
    );
    buckets.value = result.buckets;
    capacity.value = result.host_total_capacity ?? null;
  } catch (err) {
    error.value = String(err);
    buckets.value = [];
    capacity.value = null;
  } finally {
    loading.value = false;
  }
}

onMounted(fetchData);
watch(() => [props.host, props.metric, props.windowHours, props.bucketSeconds], fetchData);

// ---------------------------------------------------------------------------
// Derived: unique batch ids + per-batch summed contribution
// ---------------------------------------------------------------------------

const batchIds = computed<string[]>(() => {
  const ids = new Set<string>();
  for (const b of buckets.value) {
    for (const id of Object.keys(b.by_batch)) ids.add(id);
  }
  return Array.from(ids).sort();
});

const windowTotalAvg = computed<number | null>(() => {
  const totals = buckets.value.map((b) => b.total).filter((v): v is number => v !== null);
  if (!totals.length) return null;
  const avg = totals.reduce((a, b) => a + b, 0) / totals.length;
  const cap = capacity.value;
  if (cap == null || cap === 0) return null;
  return Math.round((avg / cap) * 100);
});

// ---------------------------------------------------------------------------
// ECharts option
// ---------------------------------------------------------------------------

const COLORS = [
  '#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de',
  '#3ba272', '#fc8452', '#9a60b4', '#ea7ccc',
];

const chartEl = ref<HTMLElement | null>(null);

const option = computed(() => {
  if (!buckets.value.length) return null;

  const xs = buckets.value.map((b) => dayjs(b.ts).format('MM-DD HH:mm'));
  const totals = buckets.value.map((b) => b.total ?? null);

  // One stacked area series per batch_id.
  const batchSeries = batchIds.value.map((bid, i) => ({
    name: bid,
    type: 'line' as const,
    stack: 'resource',
    areaStyle: { opacity: 0.6 },
    smooth: true,
    showSymbol: false,
    color: COLORS[i % COLORS.length],
    data: buckets.value.map((b) => b.by_batch[bid] ?? null),
  }));

  // Dashed total line — host-wide measurement.
  const totalSeries = {
    name: t('component_stacked_resource_chart.series_total'),
    type: 'line' as const,
    lineStyle: { type: 'dashed' as const, width: 2 },
    showSymbol: false,
    color: '#ffffff',
    data: totals,
    z: 10,
  };

  // Capacity mark line (only when capacity is known).
  const markLine =
    capacity.value != null
      ? {
          markLine: {
            data: [{ yAxis: capacity.value, name: t('component_stacked_resource_chart.capacity') }],
            lineStyle: { color: '#ff4500', type: 'dashed' as const },
            label: { formatter: t('component_stacked_resource_chart.capacity') },
          },
        }
      : {};

  const metricLabel = {
    gpu_mem_mb: 'MB',
    gpu_util_pct: '%',
    cpu_util_pct: '%',
    ram_mb: 'MB',
  }[props.metric] ?? '';

  return {
    backgroundColor: 'transparent',
    grid: { left: 70, right: 30, top: 50, bottom: 60 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
    },
    legend: {
      top: 4,
      data: [...batchIds.value, t('component_stacked_resource_chart.series_total')],
      textStyle: { fontSize: 11 },
    },
    xAxis: {
      type: 'category' as const,
      data: xs,
      axisLabel: { rotate: 30, fontSize: 10 },
    },
    yAxis: {
      type: 'value' as const,
      name: metricLabel,
      nameLocation: 'middle' as const,
      nameGap: 50,
    },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series: [
      ...batchSeries.map((s) => ({ ...s, ...markLine })),
      totalSeries,
    ],
  };
});

useChart(chartEl, option);
</script>

<template>
  <div class="stacked-resource-chart">
    <!-- Loading / error states -->
    <div v-if="loading" class="chart-state">
      {{ t('common.loading') }}
    </div>
    <div v-else-if="error" class="chart-state chart-error">
      {{ t('component_stacked_resource_chart.error') }}: {{ error }}
    </div>
    <div v-else-if="!buckets.length" class="chart-state">
      {{ t('component_stacked_resource_chart.empty') }}
    </div>

    <!-- Chart canvas — always rendered so ECharts can init -->
    <div
      ref="chartEl"
      :style="{ width: '100%', height: (height ?? 320) + 'px' }"
      :class="{ 'chart-hidden': loading || !!error || !buckets.length }"
    />

    <!-- Caption -->
    <p v-if="windowTotalAvg !== null" class="chart-caption">
      {{ t('component_stacked_resource_chart.caption', { n: windowTotalAvg }) }}
    </p>
  </div>
</template>

<style scoped>
.stacked-resource-chart {
  position: relative;
}
.chart-state {
  padding: 32px;
  text-align: center;
  color: var(--color-text-2, #888);
  font-size: 13px;
}
.chart-error {
  color: var(--color-danger, #ee6666);
}
.chart-hidden {
  display: none;
}
.chart-caption {
  text-align: center;
  font-size: 12px;
  color: var(--color-text-2, #888);
  margin: 4px 0 0;
}
</style>
