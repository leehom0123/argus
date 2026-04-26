<script setup lang="ts">
/**
 * Persistent banner shown to anonymous visitors browsing the /demo/*
 * or /public/:slug routes. Points them at /register and /login.
 *
 * Dismissable per-tab — we deliberately keep it in-memory only; the
 * next page load brings it back so the CTA stays visible across a
 * multi-page exploration of the demo.
 */
import { ref } from 'vue';
import { useI18n } from 'vue-i18n';

defineProps<{
  /** Override text (rare — normally the i18n default is fine). */
  compact?: boolean;
}>();

const { t } = useI18n();
const dismissed = ref(false);
</script>

<template>
  <a-alert
    v-if="!dismissed"
    type="info"
    show-icon
    :closable="true"
    banner
    class="anonymous-cta-banner"
    :style="compact ? { marginBottom: '8px' } : { marginBottom: '16px' }"
    @close="dismissed = true"
  >
    <template #message>
      {{ t('component_anonymous_cta.message') }}
    </template>
    <template #description>
      <span>{{ t('component_anonymous_cta.description') }}</span>
      <router-link to="/register" style="margin-left: 8px">
        <a-button type="primary" size="small">
          {{ t('component_anonymous_cta.sign_up') }}
        </a-button>
      </router-link>
      <router-link to="/login" style="margin-left: 8px">
        <a-button size="small">
          {{ t('component_anonymous_cta.sign_in') }}
        </a-button>
      </router-link>
    </template>
  </a-alert>
</template>
