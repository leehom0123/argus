<script setup lang="ts">
/**
 * /admin/users — list every user, filter by username/email/state, and ban or
 * unban individual accounts.
 */

import { computed, onMounted, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import {
  CheckCircleTwoTone,
  CloseCircleTwoTone,
  ReloadOutlined,
  StopOutlined,
  UndoOutlined,
} from '@ant-design/icons-vue';
import { banUser, listUsers, unbanUser } from '../../api/admin';
import { useAuthStore } from '../../store/auth';
import type { AdminUser } from '../../types';
import { fmtTime, fmtRelative } from '../../utils/format';

const { t } = useI18n();
const auth = useAuthStore();

const users = ref<AdminUser[]>([]);
const loading = ref(false);
const search = ref('');
const stateFilter = ref<'all' | 'active' | 'banned'>('all');

const columns = computed(() => [
  { title: t('page_admin_users.col_id'), dataIndex: 'id', key: 'id', width: 70 },
  { title: t('page_admin_users.col_username'), dataIndex: 'username', key: 'username', width: 160 },
  { title: t('page_admin_users.col_email'), dataIndex: 'email', key: 'email' },
  { title: t('page_admin_users.col_admin'), key: 'is_admin', width: 90 },
  { title: t('page_admin_users.col_active'), key: 'is_active', width: 140 },
  { title: t('page_admin_users.col_verified'), key: 'email_verified', width: 100, align: 'center' as const },
  { title: t('page_admin_users.col_created'), key: 'created_at', width: 180 },
  { title: t('page_admin_users.col_last_login'), key: 'last_login', width: 160 },
  { title: t('page_admin_users.col_actions'), key: 'actions', width: 120, fixed: 'right' as const },
]);

async function fetchAll(): Promise<void> {
  loading.value = true;
  try {
    users.value = (await listUsers()) ?? [];
  } catch {
    // interceptor notifies
  } finally {
    loading.value = false;
  }
}

const filtered = computed<AdminUser[]>(() => {
  const q = search.value.trim().toLowerCase();
  return users.value.filter((u) => {
    if (stateFilter.value === 'active' && !u.is_active) return false;
    if (stateFilter.value === 'banned' && u.is_active) return false;
    if (q.length > 0) {
      const hay = `${u.username} ${u.email}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
});

const counts = computed(() => {
  const total = users.value.length;
  const active = users.value.filter((u) => u.is_active).length;
  return { total, active, banned: total - active };
});

function isSelf(u: AdminUser): boolean {
  return auth.currentUser?.id === u.id;
}

async function doBan(u: AdminUser): Promise<void> {
  if (isSelf(u)) return;
  try {
    await banUser(u.id);
    notification.success({ message: t('page_admin_users.banned_success', { username: u.username }), duration: 2 });
    u.is_active = false;
    void fetchAll();
  } catch {
    // interceptor notified
  }
}

async function doUnban(u: AdminUser): Promise<void> {
  try {
    await unbanUser(u.id);
    notification.success({
      message: t('page_admin_users.unbanned_success', { username: u.username }),
      description: t('page_admin_users.unbanned_desc'),
      duration: 3,
    });
    u.is_active = true;
    void fetchAll();
  } catch {
    // interceptor notified
  }
}

onMounted(fetchAll);
</script>

<template>
  <div class="page-container" style="max-width: 1280px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_admin_users.breadcrumb_admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_admin_users.breadcrumb_users') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="$t('page_admin_users.card_title')">
      <template #extra>
        <a-space>
          <span class="muted" style="font-size: 12px">
            {{ $t('page_admin_users.counts', { total: counts.total, active: counts.active, banned: counts.banned }) }}
          </span>
          <a-button :loading="loading" @click="fetchAll">
            <template #icon><ReloadOutlined /></template>
            {{ $t('common.refresh') }}
          </a-button>
        </a-space>
      </template>

      <a-tabs v-model:active-key="stateFilter" size="small" style="margin-bottom: 8px">
        <a-tab-pane key="all" :tab="$t('page_admin_users.tab_all')" />
        <a-tab-pane key="active" :tab="$t('page_admin_users.tab_active')" />
        <a-tab-pane key="banned" :tab="$t('page_admin_users.tab_banned')" />
      </a-tabs>

      <div class="filter-bar">
        <a-input-search
          v-model:value="search"
          :placeholder="$t('page_admin_users.search_placeholder')"
          style="width: 280px"
          allow-clear
        />
        <span style="flex: 1" />
      </div>

      <a-table
        :columns="columns"
        :data-source="filtered"
        :loading="loading"
        row-key="id"
        size="small"
        :scroll="{ x: 1200 }"
        :pagination="{ pageSize: 20, showSizeChanger: true }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'is_admin'">
            <a-tag v-if="(record as AdminUser).is_admin" color="gold">{{ $t('page_admin_users.tag_admin') }}</a-tag>
            <span v-else class="muted">—</span>
          </template>

          <template v-else-if="column.key === 'is_active'">
            <a-tag v-if="(record as AdminUser).is_active" color="green">{{ $t('page_admin_users.tag_active') }}</a-tag>
            <a-tag v-else color="red">{{ $t('page_admin_users.tag_banned') }}</a-tag>
            <a-tag v-if="isSelf(record as AdminUser)" color="blue" style="margin-left: 4px">
              {{ $t('page_admin_users.tag_you') }}
            </a-tag>
          </template>

          <template v-else-if="column.key === 'email_verified'">
            <a-tooltip
              :title="
                (record as AdminUser).email_verified
                  ? $t('page_admin_users.tooltip_email_verified')
                  : $t('page_admin_users.tooltip_email_not_verified')
              "
            >
              <CheckCircleTwoTone
                v-if="(record as AdminUser).email_verified"
                two-tone-color="#52c41a"
              />
              <CloseCircleTwoTone v-else two-tone-color="#bfbfbf" />
            </a-tooltip>
          </template>

          <template v-else-if="column.key === 'created_at'">
            <div style="line-height: 1.2">
              <div>{{ fmtTime((record as AdminUser).created_at) }}</div>
              <div class="muted" style="font-size: 11px">
                {{ fmtRelative((record as AdminUser).created_at) }}
              </div>
            </div>
          </template>

          <template v-else-if="column.key === 'last_login'">
            <span v-if="(record as AdminUser).last_login">
              {{ fmtRelative((record as AdminUser).last_login) }}
            </span>
            <span v-else class="muted">{{ $t('page_admin_users.never_logged_in') }}</span>
          </template>

          <template v-else-if="column.key === 'actions'">
            <template v-if="(record as AdminUser).is_active">
              <a-tooltip
                v-if="isSelf(record as AdminUser)"
                :title="$t('page_admin_users.tooltip_cant_ban_self')"
              >
                <a-button size="small" danger disabled>
                  <template #icon><StopOutlined /></template>
                  {{ $t('page_admin_users.btn_ban') }}
                </a-button>
              </a-tooltip>
              <a-popconfirm
                v-else
                :title="$t('page_admin_users.popconfirm_ban', { username: (record as AdminUser).username })"
                :ok-text="$t('page_admin_users.popconfirm_ban_ok')"
                :cancel-text="$t('common.cancel')"
                ok-type="danger"
                @confirm="doBan(record as AdminUser)"
              >
                <a-button size="small" danger>
                  <template #icon><StopOutlined /></template>
                  {{ $t('page_admin_users.btn_ban') }}
                </a-button>
              </a-popconfirm>
            </template>
            <template v-else>
              <a-popconfirm
                :title="$t('page_admin_users.popconfirm_unban', { username: (record as AdminUser).username })"
                :ok-text="$t('page_admin_users.popconfirm_unban_ok')"
                :cancel-text="$t('common.cancel')"
                @confirm="doUnban(record as AdminUser)"
              >
                <a-button size="small" type="primary">
                  <template #icon><UndoOutlined /></template>
                  {{ $t('page_admin_users.btn_unban') }}
                </a-button>
              </a-popconfirm>
            </template>
          </template>
        </template>

        <template #emptyText>
          <div v-if="!loading" class="empty-wrap">
            <div style="font-size: 15px; margin-bottom: 6px">{{ $t('page_admin_users.empty_title') }}</div>
            <div class="muted" style="max-width: 480px; margin: 0 auto">
              {{ $t('page_admin_users.empty_hint') }}
            </div>
          </div>
        </template>
      </a-table>
    </a-card>
  </div>
</template>
