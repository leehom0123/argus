<script setup lang="ts">
import { ref, computed } from 'vue';
import { useChart } from '../composables/useChart';
import type { EpochPoint } from '../types';

const props = defineProps<{
  epochs: EpochPoint[];
  height?: number;
}>();

const chartEl = ref<HTMLElement | null>(null);

const option = computed(() => {
  const xs = props.epochs.map((e) => e.epoch);
  const train = props.epochs.map((e) => (e.train_loss ?? null) as number | null);
  const val = props.epochs.map((e) => (e.val_loss ?? null) as number | null);
  return {
    backgroundColor: 'transparent',
    grid: { left: 50, right: 20, top: 36, bottom: 40 },
    tooltip: { trigger: 'axis' },
    legend: { data: ['train_loss', 'val_loss'], top: 4 },
    xAxis: {
      type: 'category',
      data: xs,
      name: 'epoch',
      nameLocation: 'middle',
      nameGap: 25,
    },
    yAxis: {
      type: 'value',
      name: 'loss',
      scale: true,
    },
    series: [
      { name: 'train_loss', type: 'line', data: train, smooth: true, showSymbol: false },
      { name: 'val_loss', type: 'line', data: val, smooth: true, showSymbol: false },
    ],
  };
});

useChart(chartEl, option);
</script>

<template>
  <div ref="chartEl" :style="{ width: '100%', height: (height ?? 320) + 'px' }" />
</template>
