<script setup lang="ts">
import { ref, computed } from 'vue';
import { useI18n } from 'vue-i18n';
import dayjs from 'dayjs';
import { useChart } from '../composables/useChart';
import type { ResourceSnapshot } from '../types';

const props = defineProps<{
  snapshots: ResourceSnapshot[];
  height?: number;
  showProcessLines?: boolean;
}>();

const { t } = useI18n();

// Default showProcessLines to true when not specified.
const showProc = computed(() => props.showProcessLines !== false);

const chartEl = ref<HTMLElement | null>(null);

// Check whether the snapshot series carries any non-null proc_* values.
const hasProcData = computed(() =>
  props.snapshots.some(
    (s) =>
      (s as Record<string, unknown>).proc_cpu_pct != null ||
      (s as Record<string, unknown>).proc_ram_mb != null ||
      (s as Record<string, unknown>).proc_gpu_mem_mb != null,
  ),
);

// Caption values: latest snapshot with proc data.
const latestProcSnap = computed(() => {
  if (!hasProcData.value) return null;
  const sorted = [...props.snapshots].sort(
    (a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp),
  );
  return sorted.find((s) => (s as Record<string, unknown>).proc_gpu_mem_mb != null) ?? null;
});

const captionText = computed(() => {
  const snap = latestProcSnap.value;
  if (!snap) return '';
  const procMb = (snap as Record<string, unknown>).proc_gpu_mem_mb as number | null;
  const totalMb = snap.gpu_mem_total_mb;
  if (procMb == null) return '';
  const pct =
    totalMb != null && totalMb > 0
      ? ` (${((procMb / totalMb) * 100).toFixed(1)}%)`
      : '';
  const totalStr = totalMb != null ? ` / ${totalMb} MB` : '';
  return t('component_resource_chart.proc_caption', {
    proc: procMb,
    total: totalStr,
    pct,
  });
});

const option = computed(() => {
  const sorted = [...props.snapshots].sort(
    (a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp),
  );
  const xs = sorted.map((s) => dayjs(s.timestamp).format('MM-DD HH:mm'));

  const gpuUtil = sorted.map((s) => (s.gpu_util_pct ?? null) as number | null);
  const gpuMem = sorted.map((s) => (s.gpu_mem_mb ?? null) as number | null);
  const cpuUtil = sorted.map((s) => (s.cpu_util_pct ?? null) as number | null);
  const ram = sorted.map((s) => (s.ram_mb ?? null) as number | null);

  const procGpuMem = sorted.map(
    (s) => ((s as Record<string, unknown>).proc_gpu_mem_mb ?? null) as number | null,
  );
  const procCpuUtil = sorted.map(
    (s) => ((s as Record<string, unknown>).proc_cpu_pct ?? null) as number | null,
  );
  const procRam = sorted.map(
    (s) => ((s as Record<string, unknown>).proc_ram_mb ?? null) as number | null,
  );

  const drawProc = showProc.value && hasProcData.value;

  const hostLabel = t('component_resource_chart.host_label');
  const batchLabel = t('component_resource_chart.batch_label');

  const legendData: string[] = [
    `gpu_util % (${hostLabel})`,
    `gpu_mem MB (${hostLabel})`,
    `cpu_util % (${hostLabel})`,
    `ram MB (${hostLabel})`,
  ];
  if (drawProc) {
    legendData.push(
      `gpu_mem MB (${batchLabel})`,
      `cpu_util % (${batchLabel})`,
      `ram MB (${batchLabel})`,
    );
  }

  const series: object[] = [
    {
      name: `gpu_util % (${hostLabel})`,
      type: 'line',
      data: gpuUtil,
      yAxisIndex: 0,
      smooth: true,
      showSymbol: false,
      lineStyle: { type: 'solid' },
    },
    {
      name: `gpu_mem MB (${hostLabel})`,
      type: 'line',
      data: gpuMem,
      yAxisIndex: 1,
      smooth: true,
      showSymbol: false,
      lineStyle: { type: 'solid' },
    },
    {
      name: `cpu_util % (${hostLabel})`,
      type: 'line',
      data: cpuUtil,
      yAxisIndex: 0,
      smooth: true,
      showSymbol: false,
      lineStyle: { type: 'solid' },
    },
    {
      name: `ram MB (${hostLabel})`,
      type: 'line',
      data: ram,
      yAxisIndex: 1,
      smooth: true,
      showSymbol: false,
      lineStyle: { type: 'solid' },
    },
  ];

  if (drawProc) {
    series.push(
      {
        name: `gpu_mem MB (${batchLabel})`,
        type: 'line',
        data: procGpuMem,
        yAxisIndex: 1,
        smooth: true,
        showSymbol: false,
        lineStyle: { type: 'dashed', opacity: 0.8 },
      },
      {
        name: `cpu_util % (${batchLabel})`,
        type: 'line',
        data: procCpuUtil,
        yAxisIndex: 0,
        smooth: true,
        showSymbol: false,
        lineStyle: { type: 'dashed', opacity: 0.8 },
      },
      {
        name: `ram MB (${batchLabel})`,
        type: 'line',
        data: procRam,
        yAxisIndex: 1,
        smooth: true,
        showSymbol: false,
        lineStyle: { type: 'dashed', opacity: 0.8 },
      },
    );
  }

  return {
    backgroundColor: 'transparent',
    grid: { left: 60, right: 60, top: 40, bottom: 60 },
    tooltip: { trigger: 'axis' },
    legend: { top: 4, data: legendData },
    xAxis: { type: 'category', data: xs, axisLabel: { rotate: 30 } },
    yAxis: [
      { type: 'value', name: t('component_resource_chart.percent'), max: 100, position: 'left' },
      { type: 'value', name: t('component_resource_chart.mb'), position: 'right' },
    ],
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series,
  };
});

useChart(chartEl, option);
</script>

<template>
  <div>
    <div ref="chartEl" :style="{ width: '100%', height: (height ?? 380) + 'px' }" />
    <p
      v-if="showProc !== false && captionText"
      style="text-align: center; font-size: 0.75rem; opacity: 0.7; margin-top: 4px;"
    >
      {{ captionText }}
    </p>
  </div>
</template>
