<script setup lang="ts">
import { computed } from 'vue';

/**
 * Single metric "tile" on the dashboard top strip. Renders a large number,
 * a label, an optional trend/delta, and an optional icon. Cards are clickable
 * when `to` is set — the parent wires the route.
 */
const props = withDefaults(
  defineProps<{
    label: string;
    value: number | string | null | undefined;
    /** Tooltip / sub-label text below the big number. */
    hint?: string | null;
    /** Colour tag for the big number. */
    tone?: 'default' | 'success' | 'warn' | 'danger' | 'info';
    /** If set, clicking the card emits the `click` event. */
    clickable?: boolean;
    /** Show a small loading skeleton instead of the value. */
    loading?: boolean;
    /** Optional short suffix like "%" or "h". */
    suffix?: string | null;
  }>(),
  {
    hint: null,
    tone: 'default',
    clickable: false,
    loading: false,
    suffix: null,
  },
);

const emit = defineEmits<{ (e: 'click'): void }>();

// Tone keywords map to the AntD status palette. The status colours
// (#52c41a / #fa8c16 / #ff4d4f / #4096ff) carry enough contrast on both
// light and dark canvases to clear WCAG AA, so they stay literal. The
// ``default`` tone used to be #e6e6e6 — invisible on the light theme's
// white card surface (#120). Switching to the theme-aware ``--text-primary``
// custom property lets it adapt to either algorithm without tipping the
// status palette.
const toneColor = computed(() => {
  switch (props.tone) {
    case 'success':
      return '#52c41a';
    case 'warn':
      return '#fa8c16';
    case 'danger':
      return '#ff4d4f';
    case 'info':
      return '#4096ff';
    default:
      return 'var(--text-primary)';
  }
});

const displayValue = computed(() => {
  if (props.value === null || props.value === undefined) return '—';
  if (typeof props.value === 'number') {
    if (!Number.isFinite(props.value)) return '—';
    if (Math.abs(props.value) >= 1000) return props.value.toLocaleString();
    return String(props.value);
  }
  return String(props.value);
});

function onClick() {
  if (props.clickable) emit('click');
}
</script>

<template>
  <a-card
    size="small"
    :hoverable="clickable"
    :bodyStyle="{ padding: '12px 14px' }"
    :style="{ cursor: clickable ? 'pointer' : 'default' }"
    @click="onClick"
  >
    <div class="muted" style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px">
      {{ label }}
    </div>
    <div
      v-if="!loading"
      :style="{
        fontSize: '26px',
        fontWeight: 600,
        lineHeight: 1.2,
        color: toneColor,
        marginTop: '4px',
      }"
    >
      {{ displayValue }}<span v-if="suffix" style="font-size: 14px; margin-left: 2px">{{ suffix }}</span>
    </div>
    <a-spin v-else size="small" style="margin-top: 6px" />
    <div v-if="hint" class="muted" style="font-size: 11px; margin-top: 2px">
      {{ hint }}
    </div>
  </a-card>
</template>
