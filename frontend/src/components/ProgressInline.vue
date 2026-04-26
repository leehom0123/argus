<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
const { t } = useI18n();

const props = withDefaults(
  defineProps<{
    done: number;
    total: number;
    failed?: number;
    width?: number | string;
  }>(),
  {
    failed: 0,
    width: 140,
  },
);

const percent = computed(() =>
  props.total > 0 ? Math.round((props.done / props.total) * 100) : 0,
);

const status = computed<'normal' | 'exception' | 'success' | 'active'>(() => {
  if (props.failed && props.failed > 0) return 'exception';
  if (props.total > 0 && props.done >= props.total) return 'success';
  return 'active';
});

const widthStyle = computed(() =>
  typeof props.width === 'number' ? `${props.width}px` : props.width,
);
</script>

<template>
  <div style="display: flex; align-items: center; gap: 8px; min-width: 0">
    <a-progress
      :percent="percent"
      :status="status"
      size="small"
      :show-info="false"
      :style="{ width: widthStyle }"
    />
    <span class="muted" style="font-size: 12px; white-space: nowrap">
      {{ done }}/{{ total }}<span v-if="failed"> (<span style="color: #ff7875">{{ $t('component_progress_inline.failed', { count: failed }) }}</span>)</span>
    </span>
  </div>
</template>
