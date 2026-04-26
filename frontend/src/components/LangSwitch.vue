<template>
  <a-button size="small" @click="toggle">
    {{ currentLabel }}
  </a-button>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import { setLocale, type Locale } from '@/i18n';

const { locale } = useI18n();

const currentLabel = computed(() =>
  locale.value === 'zh-CN' ? 'EN' : '中文',
);

function toggle(): void {
  const next: Locale = locale.value === 'zh-CN' ? 'en-US' : 'zh-CN';
  setLocale(next);
}
</script>
