<script setup lang="ts">
/**
 * /admin/backups — database backup status banner (roadmap #34).
 *
 * Fetches ``GET /api/admin/backup-status`` and surfaces:
 *   - Freshness banner  →  green  (backup_age_h < interval_h)
 *                          amber  (interval_h ≤ age < 3 × interval_h)
 *                          red    (age ≥ 3 × interval_h  OR  no backup yet)
 *                          info   (enabled: false)
 *   - Recent files table (name / size / mtime).
 *
 * Read-only — no destructive actions. Triggering a manual backup isn't part
 * of this ticket; users shell into the host for that.
 */
import { computed, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { ReloadOutlined } from '@ant-design/icons-vue';
import {
  getBackupStatus,
  type BackupFile,
  type BackupStatus,
} from '../../api/admin';
import { fmtTime, fmtRelative, fmtBytes } from '../../utils/format';

const { t } = useI18n();

const status = ref<BackupStatus | null>(null);
const loading = ref(false);

async function fetchStatus(): Promise<void> {
  loading.value = true;
  try {
    status.value = await getBackupStatus();
  } catch {
    // interceptor already toasted
  } finally {
    loading.value = false;
  }
}

/** Ant Design alert level for the top banner. */
const bannerType = computed<'success' | 'warning' | 'error' | 'info'>(() => {
  const s = status.value;
  if (!s) return 'info';
  if (!s.enabled) return 'info';
  if (s.backup_age_h == null) return 'error';
  if (s.backup_age_h < s.interval_h) return 'success';
  if (s.backup_age_h < 3 * s.interval_h) return 'warning';
  return 'error';
});

const bannerMessage = computed<string>(() => {
  const s = status.value;
  if (!s) return t('page_admin.backups.banner_loading');
  if (!s.enabled) return t('page_admin.backups.banner_disabled');
  if (s.backup_age_h == null) return t('page_admin.backups.banner_no_backup');
  // "Last backup: 2.3 hours ago" — the number ships with 1 decimal place.
  return t('page_admin.backups.banner_last_backup', {
    hours: s.backup_age_h.toFixed(1),
  });
});

const bannerDescription = computed<string>(() => {
  const s = status.value;
  if (!s) return '';
  if (!s.enabled) return t('page_admin.backups.banner_disabled_desc');
  return t('page_admin.backups.banner_settings', {
    interval: s.interval_h,
    keep: s.keep_last_n,
  });
});

const columns = computed(() => [
  {
    title: t('page_admin.backups.col_name'),
    dataIndex: 'name',
    key: 'name',
  },
  {
    title: t('page_admin.backups.col_size'),
    key: 'size',
    width: 120,
  },
  {
    title: t('page_admin.backups.col_mtime'),
    key: 'mtime',
    width: 240,
  },
]);

onMounted(fetchStatus);
</script>

<template>
  <div class="page-container" style="max-width: 960px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_admin.backups.breadcrumb_admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_admin.backups.breadcrumb_backups') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="$t('page_admin.backups.card_title')">
      <template #extra>
        <a-button :loading="loading" @click="fetchStatus">
          <template #icon><ReloadOutlined /></template>
          {{ $t('common.refresh') }}
        </a-button>
      </template>

      <a-alert
        :type="bannerType"
        :message="bannerMessage"
        :description="bannerDescription || undefined"
        show-icon
        style="margin-bottom: 16px"
      />

      <a-descriptions
        v-if="status"
        :column="2"
        size="small"
        bordered
        style="margin-bottom: 20px"
      >
        <a-descriptions-item :label="$t('page_admin.backups.field_enabled')">
          <a-tag v-if="status.enabled" color="green">
            {{ $t('page_admin.backups.tag_on') }}
          </a-tag>
          <a-tag v-else color="default">
            {{ $t('page_admin.backups.tag_off') }}
          </a-tag>
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_admin.backups.field_interval')">
          {{ $t('page_admin.backups.value_interval', { h: status.interval_h }) }}
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_admin.backups.field_keep_last_n')">
          {{ status.keep_last_n }}
        </a-descriptions-item>
        <a-descriptions-item :label="$t('page_admin.backups.field_last_backup_at')">
          <template v-if="status.last_backup_at">
            {{ fmtTime(status.last_backup_at) }}
            <span class="muted" style="font-size: 11px; margin-left: 6px">
              {{ fmtRelative(status.last_backup_at) }}
            </span>
          </template>
          <span v-else class="muted">{{ $t('page_admin.backups.value_never') }}</span>
        </a-descriptions-item>
      </a-descriptions>

      <div style="font-weight: 500; margin-bottom: 8px">
        {{ $t('page_admin.backups.recent_files_title') }}
      </div>
      <a-table
        :columns="columns"
        :data-source="status?.recent_files ?? []"
        :loading="loading"
        row-key="name"
        size="small"
        :pagination="false"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'size'">
            <span style="font-family: monospace; font-size: 12px">
              {{ fmtBytes((record as BackupFile).size_bytes) }}
            </span>
          </template>
          <template v-else-if="column.key === 'mtime'">
            <div style="line-height: 1.2">
              <div>{{ fmtTime((record as BackupFile).mtime) }}</div>
              <div class="muted" style="font-size: 11px">
                {{ fmtRelative((record as BackupFile).mtime) }}
              </div>
            </div>
          </template>
        </template>

        <template #emptyText>
          <div class="empty-wrap" style="padding: 24px 0">
            <div class="muted">{{ $t('page_admin.backups.empty_files') }}</div>
          </div>
        </template>
      </a-table>
    </a-card>
  </div>
</template>
