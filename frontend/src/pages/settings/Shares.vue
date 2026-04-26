<script setup lang="ts">
/**
 * Overview of what the current user has shared out.
 */

import { computed, onMounted, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { CopyOutlined, DeleteOutlined, LinkOutlined, ReloadOutlined } from '@ant-design/icons-vue';
import { listBatches } from '../../api/client';
import {
  listBatchShares,
  listProjectShares,
  removeBatchShare,
  removeProjectShare,
} from '../../api/shares';
import {
  listBatchPublicShares,
  listMyPublicShares,
  revokePublicShare,
} from '../../api/public';
import type { BatchShare, ProjectShare, PublicShare, SharePermission } from '../../types';
import { fmtTime, fmtRelative } from '../../utils/format';

type PublicShareWithBatch = PublicShare & { batch_id: string };

const { t } = useI18n();
const activeTab = ref<'out' | 'public'>('out');

const batchSharesOut = ref<BatchShare[]>([]);
const projectSharesOut = ref<ProjectShare[]>([]);
const publicShares = ref<PublicShareWithBatch[]>([]);
const loading = ref(false);

// ---- Fetches ----

async function fetchSharedByMe(): Promise<void> {
  loading.value = true;
  try {
    const projectList = await listProjectShares().catch(() => []);
    projectSharesOut.value = projectList ?? [];

    const mine = await listBatches({ scope: 'mine', limit: 500 }).catch(() => []);
    const results = await Promise.all(
      (mine ?? []).map(async (b) => {
        try {
          const shares = await listBatchShares(b.id);
          return (shares ?? []).map((s) => ({ ...s, batch_id: b.id }));
        } catch {
          return [] as BatchShare[];
        }
      }),
    );
    batchSharesOut.value = results.flat();
  } finally {
    loading.value = false;
  }
}

async function fetchPublic(): Promise<void> {
  loading.value = true;
  try {
    try {
      const combined = await listMyPublicShares();
      publicShares.value = (combined ?? []).map((p) => ({
        ...p,
        batch_id: p.batch_id,
      }));
      return;
    } catch {
      // fall through to per-batch aggregation
    }

    const mine = await listBatches({ scope: 'mine', limit: 500 }).catch(() => []);
    const results = await Promise.all(
      (mine ?? []).map(async (b) => {
        try {
          const shares = await listBatchPublicShares(b.id);
          return (shares ?? []).map((s) => ({ ...s, batch_id: b.id }));
        } catch {
          return [] as PublicShareWithBatch[];
        }
      }),
    );
    publicShares.value = results.flat();
  } finally {
    loading.value = false;
  }
}

function refresh(): void {
  if (activeTab.value === 'out') void fetchSharedByMe();
  else void fetchPublic();
}

// ---- Actions ----

async function revokeBatch(s: BatchShare): Promise<void> {
  try {
    await removeBatchShare(s.batch_id, s.grantee_id);
    notification.success({
      message: `Removed ${s.grantee_username} from ${s.batch_id}`,
      duration: 2,
    });
    batchSharesOut.value = batchSharesOut.value.filter(
      (x) => !(x.batch_id === s.batch_id && x.grantee_id === s.grantee_id),
    );
  } catch {
    // interceptor notified
  }
}

async function revokeProject(s: ProjectShare): Promise<void> {
  try {
    await removeProjectShare(s.project, s.grantee_id);
    notification.success({
      message: `Removed ${s.grantee_username} from project "${s.project}"`,
      duration: 2,
    });
    projectSharesOut.value = projectSharesOut.value.filter(
      (x) => !(x.project === s.project && x.grantee_id === s.grantee_id),
    );
  } catch {
    // interceptor notified
  }
}

async function revokePublic(p: PublicShareWithBatch): Promise<void> {
  try {
    await revokePublicShare(p.batch_id, p.slug);
    notification.success({ message: t('page_settings_shares.public_link_revoked'), duration: 2 });
    publicShares.value = publicShares.value.filter((x) => x.slug !== p.slug);
  } catch {
    // interceptor notified
  }
}

function fullPublicUrl(p: PublicShare): string {
  if (p.url?.startsWith('http')) return p.url;
  const base = typeof window !== 'undefined' ? window.location.origin : '';
  return `${base}/public/${p.slug}`;
}

async function copyUrl(p: PublicShare): Promise<void> {
  const url = fullPublicUrl(p);
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(url);
    } else {
      const ta = document.createElement('textarea');
      ta.value = url;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    notification.success({ message: t('page_settings_shares.copied'), duration: 2 });
  } catch {
    notification.error({ message: t('page_settings_shares.copy_failed'), duration: 3 });
  }
}

function permissionColor(p: SharePermission): string {
  return p === 'editor' ? 'orange' : 'blue';
}

// ---- Table columns ----

const batchColumns = computed(() => [
  { title: t('page_settings_shares.col_batch'), dataIndex: 'batch_id', key: 'batch_id' },
  { title: t('page_settings_shares.col_shared_with'), dataIndex: 'grantee_username', key: 'grantee_username', width: 160 },
  { title: t('page_settings_shares.col_permission'), key: 'permission', width: 110 },
  { title: t('page_settings_shares.col_since'), key: 'created_at', width: 150 },
  { title: '', key: 'actions', width: 80, fixed: 'right' as const },
]);

const projectColumns = computed(() => [
  { title: t('page_settings_shares.col_project'), dataIndex: 'project', key: 'project' },
  { title: t('page_settings_shares.col_shared_with'), dataIndex: 'grantee_username', key: 'grantee_username', width: 160 },
  { title: t('page_settings_shares.col_permission'), key: 'permission', width: 110 },
  { title: t('page_settings_shares.col_since'), key: 'created_at', width: 150 },
  { title: '', key: 'actions', width: 80, fixed: 'right' as const },
]);

const publicColumns = computed(() => [
  { title: t('page_settings_shares.col_batch'), dataIndex: 'batch_id', key: 'batch_id', width: 220 },
  { title: t('page_settings_shares.col_url'), key: 'url' },
  { title: t('page_settings_shares.col_expires'), key: 'expires_at', width: 160 },
  { title: t('page_settings_shares.col_views'), key: 'view_count', width: 80 },
  { title: '', key: 'actions', width: 110, fixed: 'right' as const },
]);

const noShares = computed(
  () => batchSharesOut.value.length === 0 && projectSharesOut.value.length === 0,
);

onMounted(() => {
  void fetchSharedByMe();
});
</script>

<template>
  <div class="page-container" style="max-width: 1024px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_settings_shares.breadcrumb_settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_settings_shares.breadcrumb_shares') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card>
      <template #extra>
        <a-button :loading="loading" @click="refresh">
          <template #icon><ReloadOutlined /></template>
          {{ $t('page_settings_shares.btn_refresh') }}
        </a-button>
      </template>

      <a-tabs v-model:active-key="activeTab" @change="refresh">
        <!-- ========= Shared by me ========= -->
        <a-tab-pane key="out" :tab="$t('page_settings_shares.tab_shared_by_me')">
          <a-card size="small" :title="$t('page_settings_shares.card_project_shares')" style="margin-bottom: 16px">
            <a-table
              :columns="projectColumns"
              :data-source="projectSharesOut"
              :loading="loading"
              :row-key="(r: ProjectShare) => `${r.project}:${r.grantee_id}`"
              size="small"
              :pagination="false"
              :scroll="{ x: 720 }"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'permission'">
                  <a-tag :color="permissionColor((record as ProjectShare).permission)">
                    {{ (record as ProjectShare).permission }}
                  </a-tag>
                </template>
                <template v-else-if="column.key === 'created_at'">
                  <span v-if="(record as ProjectShare).created_at">
                    {{ fmtRelative((record as ProjectShare).created_at) }}
                  </span>
                  <span v-else class="muted">—</span>
                </template>
                <template v-else-if="column.key === 'actions'">
                  <a-popconfirm
                    :title="`Revoke ${(record as ProjectShare).grantee_username}'s access?`"
                    :ok-text="$t('common.confirm')"
                    ok-type="danger"
                    @confirm="revokeProject(record as ProjectShare)"
                  >
                    <a-button size="small" danger>
                      <template #icon><DeleteOutlined /></template>
                    </a-button>
                  </a-popconfirm>
                </template>
              </template>
              <template #emptyText>
                <div class="muted empty-wrap" style="padding: 20px">
                  {{ $t('page_settings_shares.empty_project_shares') }}
                </div>
              </template>
            </a-table>
          </a-card>

          <a-card size="small" :title="$t('page_settings_shares.card_batch_shares')">
            <a-table
              :columns="batchColumns"
              :data-source="batchSharesOut"
              :loading="loading"
              :row-key="(r: BatchShare) => `${r.batch_id}:${r.grantee_id}`"
              size="small"
              :pagination="{ pageSize: 20 }"
              :scroll="{ x: 820 }"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'batch_id'">
                  <router-link
                    :to="`/batches/${encodeURIComponent((record as BatchShare).batch_id)}`"
                  >
                    {{ (record as BatchShare).batch_id }}
                  </router-link>
                </template>
                <template v-else-if="column.key === 'permission'">
                  <a-tag :color="permissionColor((record as BatchShare).permission)">
                    {{ (record as BatchShare).permission }}
                  </a-tag>
                </template>
                <template v-else-if="column.key === 'created_at'">
                  <span v-if="(record as BatchShare).created_at">
                    {{ fmtRelative((record as BatchShare).created_at) }}
                  </span>
                  <span v-else class="muted">—</span>
                </template>
                <template v-else-if="column.key === 'actions'">
                  <a-popconfirm
                    :title="`Revoke ${(record as BatchShare).grantee_username}'s access?`"
                    :ok-text="$t('common.confirm')"
                    ok-type="danger"
                    @confirm="revokeBatch(record as BatchShare)"
                  >
                    <a-button size="small" danger>
                      <template #icon><DeleteOutlined /></template>
                    </a-button>
                  </a-popconfirm>
                </template>
              </template>
              <template #emptyText>
                <div class="muted empty-wrap" style="padding: 20px">
                  {{ $t('page_settings_shares.empty_batch_shares') }}
                </div>
              </template>
            </a-table>
          </a-card>

          <div v-if="!loading && noShares" class="muted" style="margin-top: 16px; text-align: center">
            {{ $t('page_settings_shares.empty_no_shares') }}
          </div>
        </a-tab-pane>

        <!-- ========= Public links ========= -->
        <a-tab-pane key="public" :tab="$t('page_settings_shares.tab_public_links')">
          <a-alert
            type="info"
            show-icon
            :message="$t('page_settings_shares.alert_public_msg')"
            :description="$t('page_settings_shares.alert_public_desc')"
            style="margin-bottom: 16px"
          />
          <a-table
            :columns="publicColumns"
            :data-source="publicShares"
            :loading="loading"
            row-key="slug"
            size="small"
            :pagination="{ pageSize: 20 }"
            :scroll="{ x: 860 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'batch_id'">
                <router-link
                  :to="`/batches/${encodeURIComponent((record as PublicShareWithBatch).batch_id)}`"
                >
                  {{ (record as PublicShareWithBatch).batch_id }}
                </router-link>
              </template>
              <template v-else-if="column.key === 'url'">
                <code
                  style="font-size: 11px; word-break: break-all; display: inline-block; max-width: 360px"
                >
                  {{ fullPublicUrl(record as PublicShare) }}
                </code>
              </template>
              <template v-else-if="column.key === 'expires_at'">
                <span v-if="(record as PublicShare).expires_at">
                  {{ fmtTime((record as PublicShare).expires_at) }}
                </span>
                <a-tag v-else color="default">{{ $t('page_settings_shares.never_expires') }}</a-tag>
              </template>
              <template v-else-if="column.key === 'view_count'">
                {{ (record as PublicShare).view_count ?? 0 }}
              </template>
              <template v-else-if="column.key === 'actions'">
                <a-space size="small">
                  <a-tooltip :title="$t('page_settings_shares.tooltip_copy_link')">
                    <a-button size="small" @click="copyUrl(record as PublicShare)">
                      <template #icon><CopyOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-popconfirm
                    :title="$t('page_settings_shares.popconfirm_revoke')"
                    :ok-text="$t('page_settings_shares.btn_revoke')"
                    ok-type="danger"
                    @confirm="revokePublic(record as PublicShareWithBatch)"
                  >
                    <a-button size="small" danger>
                      <template #icon><DeleteOutlined /></template>
                    </a-button>
                  </a-popconfirm>
                </a-space>
              </template>
            </template>
            <template #emptyText>
              <div class="muted empty-wrap" style="padding: 20px">
                <LinkOutlined style="font-size: 24px; display: block; margin-bottom: 8px" />
                {{ $t('page_settings_shares.empty_public_links') }}
              </div>
            </template>
          </a-table>
        </a-tab-pane>
      </a-tabs>
    </a-card>
  </div>
</template>
