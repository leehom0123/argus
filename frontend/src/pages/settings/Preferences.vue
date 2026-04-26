<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { getPreferences } from '../../api/auth';

// The "show demo project" toggle was removed when demo visibility moved
// fully server-side: signed-in users no longer see demo entries at all,
// and anonymous visitors reach them via /demo/*. Preferences page now
// just displays the effective locale as a read-only summary.

const loading = ref(false);
const preferredLocale = ref<string>('en-US');

async function load() {
  loading.value = true;
  try {
    const prefs = await getPreferences();
    preferredLocale.value = prefs.preferred_locale;
  } catch {
    // axios interceptor toasts
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void load();
});
</script>

<template>
  <div class="page-container" style="max-width: 720px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_settings_preferences.breadcrumb_settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_settings_preferences.breadcrumb_preferences') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card
      :title="$t('page_settings_preferences.card_title')"
      :loading="loading"
    >
      <a-form layout="vertical">
        <a-form-item>
          <template #label>
            <span>{{ $t('page_settings_preferences.locale_label') }}</span>
          </template>
          <span>{{ preferredLocale }}</span>
          <div class="muted" style="margin-top: 6px; font-size: 12px">
            {{ $t('page_settings_preferences.locale_desc') }}
          </div>
        </a-form-item>
      </a-form>
    </a-card>
  </div>
</template>
