<script setup lang="ts">
import { useAppStore } from '../store/app';
const app = useAppStore();
</script>

<template>
  <div class="page-container" style="max-width: 640px">
    <h3 style="margin-top: 0">{{ $t('page_settings.title') }}</h3>

    <a-form layout="vertical">
      <a-form-item :label="$t('page_settings.label_theme')">
        <a-radio-group v-model:value="app.darkMode" @change="app.persist()">
          <a-radio :value="true">{{ $t('page_settings.theme_dark') }}</a-radio>
          <a-radio :value="false">{{ $t('page_settings.theme_light') }}</a-radio>
        </a-radio-group>
      </a-form-item>

      <a-form-item :label="$t('page_settings.label_auto_refresh')">
        <a-input-number
          v-model:value="app.autoRefreshSec"
          :min="5"
          :max="600"
          :step="5"
          @change="app.persist()"
        />
      </a-form-item>
    </a-form>

    <p class="muted" style="font-size: 12px">
      {{ $t('page_settings.backend_note') }}
    </p>
  </div>
</template>
