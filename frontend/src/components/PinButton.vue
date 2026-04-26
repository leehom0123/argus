<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { PushpinFilled, PushpinOutlined } from '@ant-design/icons-vue';
import { PIN_LIMIT, usePinsStore } from '../store/pins';

const { t } = useI18n();
const props = withDefaults(
  defineProps<{
    batchId: string;
    iconOnly?: boolean;
    size?: number;
    /** Show the "n/4" counter next to the icon even when not pinned. */
    showCounter?: boolean;
  }>(),
  { iconOnly: false, size: 16, showCounter: false },
);

const pins = usePinsStore();

const pinned = computed(() => pins.isPinned(props.batchId));
const disabled = computed(() => !pinned.value && pins.isFull);

async function toggle(ev?: MouseEvent) {
  ev?.stopPropagation();
  ev?.preventDefault();
  try {
    await pins.toggle(props.batchId);
  } catch {
    // store already surfaced the error
  }
}

onMounted(() => {
  void pins.ensureLoaded();
});
</script>

<template>
  <a-tooltip
    :title="
      pinned
        ? $t('component_pin_button.unpin')
        : disabled
          ? $t('component_pin_button.pool_full')
          : $t('component_pin_button.pin', { count: pins.count, limit: PIN_LIMIT })
    "
  >
    <a-button
      :type="pinned ? 'primary' : 'text'"
      :ghost="pinned"
      size="small"
      :disabled="disabled"
      @click="toggle"
    >
      <template #icon>
        <PushpinFilled
          v-if="pinned"
          :style="{ color: '#1677ff', fontSize: size + 'px' }"
        />
        <PushpinOutlined v-else :style="{ fontSize: size + 'px' }" />
      </template>
      <span v-if="!iconOnly" style="margin-left: 2px">
        {{ pinned ? $t('component_pin_button.pinned') : $t('component_pin_button.pin_label') }}
        <span v-if="showCounter" class="muted" style="margin-left: 4px">
          {{ pins.count }}/{{ PIN_LIMIT }}
        </span>
      </span>
    </a-button>
  </a-tooltip>
</template>
