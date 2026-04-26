<script setup lang="ts">
import { ref, computed } from 'vue';
import { useChart } from '../composables/useChart';

/**
 * 30px-high ECharts line. Used inside BatchCard loss traces, project
 * activity cards, etc. Zero axes / legend — just a trend line.
 */
const props = withDefaults(
  defineProps<{
    /** y-values. x is implicit 0..N-1. */
    data: (number | null)[];
    height?: number;
    /** Line colour; defaults to primary. */
    color?: string;
    /** Show a subtle fill under the line. */
    area?: boolean;
  }>(),
  {
    height: 30,
    color: '#4096ff',
    area: false,
  },
);

const chartEl = ref<HTMLElement | null>(null);

const option = computed(() => {
  const data = props.data ?? [];
  return {
    backgroundColor: 'transparent',
    grid: { left: 2, right: 2, top: 2, bottom: 2, containLabel: false },
    tooltip: { show: false },
    xAxis: {
      type: 'category',
      show: false,
      boundaryGap: false,
      data: data.map((_, i) => i),
    },
    yAxis: { type: 'value', show: false, scale: true },
    series: [
      {
        type: 'line',
        data,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1.6, color: props.color },
        areaStyle: props.area
          ? { color: props.color, opacity: 0.18 }
          : undefined,
      },
    ],
  };
});

useChart(chartEl, option);

const hasData = computed(() => (props.data?.filter((v) => v !== null && v !== undefined).length ?? 0) >= 2);
</script>

<template>
  <div
    ref="chartEl"
    :style="{ width: '100%', height: height + 'px', minWidth: '40px' }"
    :class="{ 'muted-empty': !hasData }"
  />
</template>

<style scoped>
.muted-empty {
  opacity: 0.35;
}
</style>
