<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import {
  CheckCircleFilled,
  CloseCircleFilled,
  PlayCircleFilled,
  WarningFilled,
  InfoCircleOutlined,
  HddOutlined,
} from '@ant-design/icons-vue';
import { useRouter } from 'vue-router';
import type { ActivityItem } from '../types';
import { fmtRelative } from '../utils/format';

const { t, te } = useI18n();
const props = withDefaults(
  defineProps<{
    items: ActivityItem[];
    maxItems?: number;
    /** Compact mode hides the project column on narrow rails. */
    compact?: boolean;
  }>(),
  { maxItems: 20, compact: false },
);

const router = useRouter();

const visible = computed(() => (props.items ?? []).slice(0, props.maxItems));

function iconFor(item: ActivityItem) {
  switch (item.event_type) {
    case 'batch_start':
    case 'job_start':
      return PlayCircleFilled;
    case 'batch_done':
    case 'job_done':
      return CheckCircleFilled;
    case 'batch_failed':
    case 'job_failed':
      return CloseCircleFilled;
    case 'batch_diverged':
    case 'job_idle_flagged':
      return WarningFilled;
    case 'resource_snapshot':
      return HddOutlined;
    default:
      return InfoCircleOutlined;
  }
}

function colorFor(item: ActivityItem): string {
  if (item.level === 'error') return '#ff4d4f';
  if (item.level === 'warn') return '#faad14';
  switch (item.event_type) {
    case 'batch_start':
    case 'job_start':
      return '#4096ff';
    case 'batch_done':
    case 'job_done':
      return '#52c41a';
    case 'batch_failed':
    case 'job_failed':
      return '#ff4d4f';
    case 'batch_diverged':
    case 'job_idle_flagged':
      // Guardrails advisory events share the amber "warning" tone the rest
      // of the app uses for non-fatal attention-needed states.
      return '#faad14';
    default:
      return '#a0a0a0';
  }
}

/**
 * Human-readable event label.  Falls back to "batch diverged" style spacing
 * when no translation is registered so forward-compat events still render.
 */
function friendlyType(tp: string): string {
  const key = `event_type_${tp}`;
  if (te(key)) return t(key);
  return tp.replace(/_/g, ' ');
}

function open(item: ActivityItem) {
  if (item.batch_id) {
    router.push(
      item.job_id
        ? `/batches/${encodeURIComponent(item.batch_id)}/jobs/${encodeURIComponent(item.job_id)}`
        : `/batches/${encodeURIComponent(item.batch_id)}`,
    );
  } else if (item.project) {
    router.push(`/projects/${encodeURIComponent(item.project)}`);
  }
}
</script>

<template>
  <div v-if="!visible.length" class="muted empty-wrap" style="padding: 24px">
    {{ $t('component_activity_feed.empty') }}
  </div>
  <a-timeline v-else style="padding: 4px 0 0 4px">
    <a-timeline-item
      v-for="(item, i) in visible"
      :key="item.id ?? `${item.timestamp}-${i}`"
      :color="colorFor(item)"
    >
      <template #dot>
        <component :is="iconFor(item)" :style="{ color: colorFor(item) }" />
      </template>
      <div
        style="cursor: pointer; line-height: 1.4"
        @click="() => open(item)"
      >
        <div style="font-size: 12.5px">
          <a-tag :color="colorFor(item)" style="margin-right: 6px">
            {{ friendlyType(item.event_type) }}
          </a-tag>
          <span>{{ item.message || item.batch_id || item.project || '—' }}</span>
        </div>
        <div class="muted" style="font-size: 11px; margin-top: 1px">
          <span v-if="item.user">{{ item.user }}</span>
          <span v-if="item.project && !compact"> · {{ item.project }}</span>
          <span v-if="item.batch_id"> · {{ item.batch_id }}</span>
          <span> · {{ fmtRelative(item.timestamp) }}</span>
        </div>
      </div>
    </a-timeline-item>
  </a-timeline>
</template>
