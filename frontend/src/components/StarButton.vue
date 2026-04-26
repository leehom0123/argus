<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { StarFilled, StarOutlined } from '@ant-design/icons-vue';
import { useStarsStore } from '../store/stars';

const { t } = useI18n();
const props = withDefaults(
  defineProps<{
    targetType: 'project' | 'batch';
    targetId: string;
    /** Render only the icon; skip the text. */
    iconOnly?: boolean;
    /** Size of the icon itself, in px. */
    size?: number;
  }>(),
  { iconOnly: false, size: 18 },
);

const store = useStarsStore();

const starred = computed(() => store.isStarred(props.targetType, props.targetId));

async function toggle(ev?: MouseEvent) {
  ev?.stopPropagation();
  ev?.preventDefault();
  try {
    await store.toggle(props.targetType, props.targetId);
  } catch {
    // store already logged; swallow so UI stays responsive
  }
}

onMounted(() => {
  void store.ensureLoaded();
});
</script>

<template>
  <a-tooltip :title="starred ? $t('component_star_button.unstar') : $t('component_star_button.star')">
    <a-button
      :type="starred ? 'primary' : 'text'"
      :ghost="starred"
      size="small"
      @click="toggle"
    >
      <template #icon>
        <StarFilled
          v-if="starred"
          :style="{ color: '#faad14', fontSize: size + 'px' }"
        />
        <StarOutlined v-else :style="{ fontSize: size + 'px' }" />
      </template>
      <span v-if="!iconOnly" style="margin-left: 2px">
        {{ starred ? $t('component_star_button.starred') : $t('component_star_button.star_label') }}
      </span>
    </a-button>
  </a-tooltip>
</template>
