<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';

/**
 * ``reason`` is surfaced as a tooltip — used by the ``divergent`` chip to
 * explain *why* a batch was flagged (e.g. "val_loss doubled" / "NaN"). When
 * unset the a-tag renders without a wrapper tooltip.
 */
const props = defineProps<{
  status?: string | null;
  reason?: string | null;
}>();

const { t, te } = useI18n();

// The colour preset below and the raw hex palette in utils/status.ts share
// the same source of truth via their shared 5-bucket scheme. If you add a
// status to one side, add it to both (and locales) or the test in
// __tests__/status-color.test.ts will call out the drift.
//
// 5-colour scheme (#125):
//   running  → green   (active execution)
//   failed   → red     (hard error)
//   stalled  → warning (heartbeat lost / divergent — yellow)
//   pending  → blue    (queued / waiting / requested)
//   done     → default (gray / unobtrusive)
const color = computed(() => {
  const s = (props.status ?? '').toLowerCase();
  switch (s) {
    // running family → green
    case 'running':
    case 'in_progress':
      return 'green';
    // failed family → red
    case 'failed':
    case 'error':
      return 'red';
    // stalled / warning family → warning (yellow)
    case 'divergent':
    case 'stalled':
      return 'warning';
    // pending family → blue
    case 'pending':
    case 'queued':
    case 'requested':
      return 'blue';
    // done / terminal family → default (gray)
    case 'done':
    case 'success':
    case 'completed':
    case 'stopping':
    case 'stopped':
      return 'default';
    // partial / skipped — keep their distinct presets so they remain
    // visually separable from the canonical 5 buckets.
    case 'partial':
      return 'orange';
    case 'skipped':
      return 'purple';
    default:
      return 'default';
  }
});

const label = computed(() => {
  const raw = (props.status ?? '').toString().toLowerCase();
  if (!raw) return t('component_status_tag.unknown');
  const key = `component_status_tag.status_${raw}`;
  if (te(key)) return t(key);
  return raw.toUpperCase();
});

/** Tooltip text — only rendered when we have something to explain. */
const tooltipText = computed(() => {
  const raw = (props.status ?? '').toString().toLowerCase();
  if (raw === 'divergent') {
    // Prefer explicit reason from the batch_diverged event payload; else
    // fall back to the generic "val_loss doubled / NaN" hint.
    return props.reason && props.reason.trim().length > 0
      ? props.reason
      : t('component_status_tag.tooltip_divergent');
  }
  if (raw === 'stalled') {
    return props.reason && props.reason.trim().length > 0
      ? props.reason
      : t('component_status_tag.tooltip_stalled');
  }
  return '';
});
</script>

<template>
  <a-tooltip v-if="tooltipText" :title="tooltipText">
    <a-tag :color="color">{{ label }}</a-tag>
  </a-tooltip>
  <a-tag v-else :color="color">{{ label }}</a-tag>
</template>
