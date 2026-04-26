<script setup lang="ts">
/**
 * Guardrails #13 — small amber "Idle" chip rendered next to a job's status
 * when ``job.is_idle_flagged === true``. The backend flips the flag when
 * GPU utilisation stays under 5% for ``idle_job_threshold_min`` minutes; no
 * job is killed — this is advisory only.
 *
 * Accepts an optional ``minutes`` prop so callers with the event payload
 * (``job_idle_flagged.minutes``) can show the exact window in the tooltip;
 * otherwise the generic "GPU utilization low" fallback is used.
 */
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';

const props = withDefaults(
  defineProps<{
    flagged?: boolean | null;
    /** Minutes over which GPU util was below threshold (from event payload). */
    minutes?: number | null;
  }>(),
  { flagged: false, minutes: null },
);

const { t } = useI18n();

const tooltip = computed(() =>
  props.minutes && props.minutes > 0
    ? t('component_job_badge.idle_tooltip_minutes', { minutes: props.minutes })
    : t('component_job_badge.idle_tooltip_generic'),
);
</script>

<template>
  <a-tooltip v-if="flagged" :title="tooltip">
    <a-tag color="warning" style="font-size: 11px; line-height: 16px; padding: 0 6px; margin-left: 4px">
      {{ $t('component_job_badge.idle') }}
    </a-tag>
  </a-tooltip>
</template>
