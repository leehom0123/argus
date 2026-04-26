<script setup lang="ts">
/**
 * Active sessions panel — issue #31.
 *
 * Lists every unexpired JWT the caller has issued (across devices / browsers)
 * and lets them revoke individual sessions. The current session is tagged
 * and its revoke button is disabled so users cannot accidentally sign
 * themselves out from here (they should use the header "Log out" instead,
 * which also clears the local token).
 */

import { computed, onMounted, ref } from 'vue';
import { AxiosError } from 'axios';
import { notification } from 'ant-design-vue';
import { ReloadOutlined } from '@ant-design/icons-vue';
import { useI18n } from 'vue-i18n';
import { listSessions, revokeSession } from '../../api/auth';
import type { ActiveSession } from '../../types';
import { fmtTime, fmtRelative } from '../../utils/format';

const { t } = useI18n();

const sessions = ref<ActiveSession[]>([]);
const loading = ref(false);
const revokingJti = ref<string | null>(null);

interface ParsedUA {
  browser: string | null;
  os: string | null;
}

function parseUA(ua: string | null | undefined): ParsedUA {
  if (!ua) return { browser: null, os: null };
  let browser: string | null = null;
  if (/Edg\//.test(ua)) browser = 'Edge';
  else if (/OPR\//.test(ua) || /Opera/.test(ua)) browser = 'Opera';
  else if (/Firefox\//.test(ua)) browser = 'Firefox';
  else if (/Chrome\//.test(ua)) browser = 'Chrome';
  else if (/Safari\//.test(ua) && /Version\//.test(ua)) browser = 'Safari';
  else if (/curl\//i.test(ua)) browser = 'curl';
  else if (/python-requests/i.test(ua)) browser = 'python';
  else if (/httpx/i.test(ua)) browser = 'httpx';

  let os: string | null = null;
  if (/Windows NT 10\.0/.test(ua)) os = 'Windows 10/11';
  else if (/Windows NT/.test(ua)) os = 'Windows';
  else if (/Mac OS X/.test(ua)) os = 'macOS';
  else if (/Android/.test(ua)) os = 'Android';
  else if (/iPhone|iPad/.test(ua)) os = 'iOS';
  else if (/Linux/.test(ua)) os = 'Linux';

  return { browser, os };
}

function renderUA(ua: string | null | undefined): string {
  const { browser, os } = parseUA(ua);
  if (browser && os) return `${browser} · ${os}`;
  if (browser) return browser;
  if (os) return os;
  if (ua) return ua.length > 60 ? `${ua.slice(0, 60)}…` : ua;
  return t('page_settings_sessions.ua_unknown');
}

async function fetchAll(): Promise<void> {
  loading.value = true;
  try {
    sessions.value = (await listSessions()) ?? [];
  } catch {
    // interceptor toasted
  } finally {
    loading.value = false;
  }
}

async function doRevoke(row: ActiveSession): Promise<void> {
  if (row.is_current) return;
  revokingJti.value = row.jti;
  try {
    await revokeSession(row.jti);
    sessions.value = sessions.value.filter((s) => s.jti !== row.jti);
    notification.success({ message: t('page_settings_sessions.revoked_msg'), duration: 2 });
  } catch (err) {
    if (err instanceof AxiosError && err.response?.status === 404) {
      sessions.value = sessions.value.filter((s) => s.jti !== row.jti);
      notification.info({ message: t('page_settings_sessions.already_gone_msg'), duration: 3 });
    }
  } finally {
    revokingJti.value = null;
  }
}

const columns = computed(() => [
  { title: t('page_settings_sessions.col_device'), key: 'device', width: 220 },
  { title: t('page_settings_sessions.col_ip'), key: 'ip', width: 140 },
  { title: t('page_settings_sessions.col_issued'), key: 'issued_at', width: 200 },
  { title: t('page_settings_sessions.col_last_seen'), key: 'last_seen_at', width: 160 },
  { title: t('page_settings_sessions.col_expires'), key: 'expires_at', width: 200 },
  { title: t('page_settings_sessions.col_actions'), key: 'actions', width: 140, fixed: 'right' as const },
]);

const hasSessions = computed(() => sessions.value.length > 0);

onMounted(fetchAll);
</script>

<template>
  <div class="page-container" style="max-width: 1100px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_settings_sessions.breadcrumb_settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_settings_sessions.breadcrumb_sessions') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="$t('page_settings_sessions.card_title')">
      <template #extra>
        <a-button :loading="loading" @click="fetchAll">
          <template #icon><ReloadOutlined /></template>
          {{ $t('page_settings_sessions.btn_refresh') }}
        </a-button>
      </template>

      <a-alert
        type="info"
        show-icon
        :message="$t('page_settings_sessions.alert_msg')"
        :description="$t('page_settings_sessions.alert_desc')"
        style="margin-bottom: 16px"
      />

      <a-table
        :columns="columns"
        :data-source="sessions"
        :loading="loading"
        row-key="jti"
        size="small"
        :scroll="{ x: 1000 }"
        :pagination="false"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'device'">
            <div style="line-height: 1.3">
              <div>{{ renderUA((record as ActiveSession).user_agent) }}</div>
              <a-tag
                v-if="(record as ActiveSession).is_current"
                color="green"
                style="margin-top: 4px"
              >
                {{ $t('page_settings_sessions.tag_current') }}
              </a-tag>
            </div>
          </template>

          <template v-else-if="column.key === 'ip'">
            <code style="font-size: 12px">
              {{ (record as ActiveSession).ip || $t('page_settings_sessions.ip_unknown') }}
            </code>
          </template>

          <template v-else-if="column.key === 'issued_at'">
            <div style="line-height: 1.2">
              <div>{{ fmtTime((record as ActiveSession).issued_at) }}</div>
              <div class="muted" style="font-size: 11px">
                {{ fmtRelative((record as ActiveSession).issued_at) }}
              </div>
            </div>
          </template>

          <template v-else-if="column.key === 'last_seen_at'">
            <span v-if="(record as ActiveSession).last_seen_at">
              {{ fmtRelative((record as ActiveSession).last_seen_at) }}
            </span>
            <span v-else class="muted">{{ $t('page_settings_sessions.never_seen') }}</span>
          </template>

          <template v-else-if="column.key === 'expires_at'">
            {{ fmtTime((record as ActiveSession).expires_at) }}
          </template>

          <template v-else-if="column.key === 'actions'">
            <a-tooltip
              v-if="(record as ActiveSession).is_current"
              :title="$t('page_settings_sessions.tooltip_cant_revoke_current')"
            >
              <a-button size="small" disabled>
                {{ $t('page_settings_sessions.btn_revoke') }}
              </a-button>
            </a-tooltip>
            <a-popconfirm
              v-else
              :title="$t('page_settings_sessions.popconfirm_revoke_title')"
              :description="$t('page_settings_sessions.popconfirm_revoke_desc')"
              :ok-text="$t('page_settings_sessions.btn_revoke')"
              :cancel-text="$t('common.cancel')"
              ok-type="danger"
              @confirm="doRevoke(record as ActiveSession)"
            >
              <a-button
                size="small"
                danger
                :loading="revokingJti === (record as ActiveSession).jti"
              >
                {{ $t('page_settings_sessions.btn_revoke') }}
              </a-button>
            </a-popconfirm>
          </template>
        </template>

        <template #emptyText>
          <div v-if="!loading" class="empty-wrap">
            <div style="font-size: 15px; margin-bottom: 6px">
              {{ $t('page_settings_sessions.empty_title') }}
            </div>
            <div class="muted" style="max-width: 520px; margin: 0 auto">
              {{ $t('page_settings_sessions.empty_hint') }}
            </div>
          </div>
        </template>
      </a-table>

      <div v-if="hasSessions" class="muted" style="margin-top: 10px; font-size: 12px">
        {{ $t('page_settings_sessions.showing_count', { count: sessions.length }) }}
      </div>
    </a-card>
  </div>
</template>
