<script setup lang="ts">
// Settings → About
//
// Small card that surfaces open-source footprint: version (injected at build
// time by vite's `define`), license link, docs, GitHub repo. Deliberately
// lightweight — no API calls, no auth dependency beyond the settings guard.

const appVersion = __APP_VERSION__;

const LICENSE_URL = 'https://www.apache.org/licenses/LICENSE-2.0';
const GITHUB_URL = 'https://github.com/leehom0123/argus';
const DOCS_URL =
  'https://github.com/leehom0123/argus/blob/main/docs/requirements.md';
</script>

<template>
  <div class="page-container" style="max-width: 720px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_settings_about.breadcrumb_settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_settings_about.breadcrumb_about') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="$t('page_settings_about.card_title')">
      <a-descriptions :column="1" bordered size="small">
        <a-descriptions-item :label="$t('page_settings_about.label_app_name')">
          Argus
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_settings_about.label_version')">
          <a-tag color="blue">v{{ appVersion }}</a-tag>
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_settings_about.label_license')">
          <a :href="LICENSE_URL" target="_blank" rel="noopener noreferrer">
            Apache License, Version 2.0
          </a>
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_settings_about.label_docs')">
          <a :href="DOCS_URL" target="_blank" rel="noopener noreferrer">
            {{ $t('page_settings_about.link_docs') }}
          </a>
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_settings_about.label_repo')">
          <a :href="GITHUB_URL" target="_blank" rel="noopener noreferrer">
            github.com/leehom0123/argus
          </a>
        </a-descriptions-item>
      </a-descriptions>

      <p class="muted" style="margin-top: 16px; font-size: 12px">
        {{ $t('page_settings_about.blurb') }}
      </p>
    </a-card>
  </div>
</template>
