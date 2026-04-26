<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import { InboxOutlined } from '@ant-design/icons-vue';
import { useHintsStore } from '../store/hints';

/**
 * Localised empty-state block (roadmap #30).
 *
 * Reads the hint catalog pre-loaded by the hints Pinia store (populated
 * at app mount from ``GET /api/meta/hints``). If the backend catalog is
 * unavailable, falls back to the bundled i18n copy under
 * ``component_empty_state.<variant>`` so the UI degrades gracefully.
 *
 * Callers pass ``variant`` as the bare key (``empty_hosts``) — same
 * vocabulary the backend uses so no translation layer is needed.
 *
 * Usage:
 *   <EmptyState variant="empty_hosts" />
 *   <EmptyState variant="empty_batches" :title="customTitle" />
 *   <EmptyState variant="empty_jobs">
 *     <!-- optional slot for call-to-action buttons -->
 *     <a-button>Refresh</a-button>
 *   </EmptyState>
 */
const props = withDefaults(
  defineProps<{
    /** Hint catalog key; e.g. "empty_hosts". */
    variant: string;
    /** Override the hint body text (skips catalog + i18n lookups). */
    hint?: string | null;
    /** Optional short title shown above the hint. */
    title?: string | null;
    /**
     * Render style: ``block`` pads the card nicely (default); ``inline``
     * is tight for table emptyText slots.
     */
    layout?: 'block' | 'inline';
    /** Show the inbox icon above the title. Off by default for inline. */
    icon?: boolean;
  }>(),
  {
    hint: null,
    title: null,
    layout: 'block',
    icon: true,
  },
);

const hintsStore = useHintsStore();
const { t, te } = useI18n();

/** Final text shown below the title. Tries: explicit → backend → i18n → bare key. */
const resolvedHint = computed<string>(() => {
  if (props.hint) return props.hint;
  const fromBackend = hintsStore.hint(props.variant);
  if (fromBackend) return fromBackend;
  const key = `component_empty_state.${props.variant}`;
  if (te(key)) return t(key);
  return '';
});

const showIcon = computed(() => props.icon && props.layout === 'block');
</script>

<template>
  <div
    v-if="layout === 'block'"
    class="empty-state empty-wrap"
    style="text-align: center; padding: 28px 16px"
  >
    <InboxOutlined
      v-if="showIcon"
      style="font-size: 32px; color: var(--text-quaternary); display: block; margin-bottom: 10px"
    />
    <div
      v-if="title"
      style="font-size: 15px; margin-bottom: 6px"
    >
      {{ title }}
    </div>
    <div
      v-if="resolvedHint"
      class="muted"
      style="max-width: 520px; margin: 0 auto; font-size: 13px; line-height: 1.5"
    >
      {{ resolvedHint }}
    </div>
    <div v-if="$slots.default" style="margin-top: 14px">
      <slot />
    </div>
  </div>
  <div v-else class="empty-state muted" style="padding: 12px 8px; font-size: 13px">
    <span v-if="title" style="margin-right: 8px">{{ title }}</span>
    <span v-if="resolvedHint">{{ resolvedHint }}</span>
    <span v-if="$slots.default" style="margin-left: 8px">
      <slot />
    </span>
  </div>
</template>
