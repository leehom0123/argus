<script setup lang="ts">
import { ref, computed } from 'vue';
import { useChart } from '../composables/useChart';
import type { JobMetrics } from '../types';

const props = defineProps<{
  metrics?: JobMetrics | null;
  height?: number;
}>();

const chartEl = ref<HTMLElement | null>(null);

const option = computed(() => {
  const entries = Object.entries(props.metrics ?? {})
    .filter(([, v]) => typeof v === 'number' && Number.isFinite(v))
    .slice(0, 12);
  const names = entries.map(([k]) => k);
  const values = entries.map(([, v]) => v as number);
  return {
    backgroundColor: 'transparent',
    grid: { left: 60, right: 20, top: 36, bottom: 40 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: names, axisLabel: { rotate: 30 } },
    yAxis: { type: 'value' },
    series: [
      {
        type: 'bar',
        data: values,
        itemStyle: { color: '#4096ff' },
        label: { show: true, position: 'top', formatter: (p: { value: number }) => p.value.toFixed(3) },
      },
    ],
  };
});

useChart(chartEl, option);
</script>

<template>
  <div ref="chartEl" :style="{ width: '100%', height: (height ?? 320) + 'px' }" />
</template>
