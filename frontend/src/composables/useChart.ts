import { onMounted, onBeforeUnmount, ref, watch, type Ref } from 'vue';
import * as echarts from 'echarts/core';
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
  MarkLineComponent,
} from 'echarts/components';
import { LineChart, BarChart } from 'echarts/charts';
import { CanvasRenderer } from 'echarts/renderers';
import { UniversalTransition } from 'echarts/features';

echarts.use([
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
  MarkLineComponent,
  LineChart,
  BarChart,
  CanvasRenderer,
  UniversalTransition,
]);

export function useChart(
  el: Ref<HTMLElement | null>,
  optionRef: Ref<echarts.EChartsCoreOption | null>,
) {
  let instance: echarts.ECharts | null = null;

  function resize() {
    instance?.resize();
  }

  onMounted(() => {
    if (!el.value) return;
    instance = echarts.init(el.value, 'dark');
    if (optionRef.value) instance.setOption(optionRef.value);
    window.addEventListener('resize', resize);
  });

  watch(
    optionRef,
    (val) => {
      if (instance && val) instance.setOption(val, true);
    },
    { deep: true },
  );

  onBeforeUnmount(() => {
    window.removeEventListener('resize', resize);
    instance?.dispose();
    instance = null;
  });

  return { resize };
}
