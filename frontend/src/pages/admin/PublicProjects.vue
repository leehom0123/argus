<script setup lang="ts">
/**
 * /admin/public-projects — list every project currently flagged public
 * (``ProjectMeta.is_public=True``) and expose a one-click Revoke action.
 *
 * Behind ``requiresAuth`` + ``requiresAdmin`` route-guards; the backend
 * enforces :func:`require_admin` on every call via ``/api/admin/*``.
 */

import { computed, onMounted, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import {
  ReloadOutlined,
  GlobalOutlined,
  StopOutlined,
} from '@ant-design/icons-vue';
import {
  listAdminPublicProjects,
  unpublishProject,
  type PublicProjectMeta,
} from '../../api/admin';
import { fmtRelative } from '../../utils/format';

const { t } = useI18n();
const rows = ref<PublicProjectMeta[]>([]);
const loading = ref(false);

const columns = computed(() => [
  { title: t('page_admin_public_projects.col_project'), dataIndex: 'project', key: 'project', width: 240 },
  { title: t('page_admin_public_projects.col_description'), dataIndex: 'public_description', key: 'description' },
  { title: t('page_admin_public_projects.col_published'), key: 'published_at', width: 160 },
  { title: t('page_admin_public_projects.col_published_by'), dataIndex: 'published_by_user_id', key: 'published_by_user_id', width: 80 },
  { title: t('page_admin_public_projects.col_actions'), key: 'actions', width: 140, fixed: 'right' as const },
]);

async function fetchAll(): Promise<void> {
  loading.value = true;
  try {
    rows.value = (await listAdminPublicProjects()) ?? [];
  } catch {
    // interceptor notifies
  } finally {
    loading.value = false;
  }
}

async function doRevoke(row: PublicProjectMeta): Promise<void> {
  try {
    await unpublishProject(row.project);
    notification.success({
      message: t('page_admin_public_projects.revoked_toast'),
      duration: 2,
    });
    void fetchAll();
  } catch {
    // interceptor notifies
  }
}

onMounted(fetchAll);
</script>

<template>
  <div class="page-container" style="max-width: 1200px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ t('page_admin_public_projects.breadcrumb') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('page_admin_public_projects.breadcrumb_public') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="t('page_admin_public_projects.card_title')">
      <template #extra>
        <a-button :loading="loading" @click="fetchAll">
          <template #icon><ReloadOutlined /></template>
          {{ t('common.refresh') }}
        </a-button>
      </template>

      <a-alert
        type="info"
        show-icon
        :message="t('page_admin_public_projects.hint')"
        style="margin-bottom: 14px"
      >
        <template #icon><GlobalOutlined /></template>
      </a-alert>

      <a-table
        :columns="columns"
        :data-source="rows"
        :loading="loading"
        row-key="project"
        size="small"
        :scroll="{ x: 1000 }"
        :pagination="{ pageSize: 20 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'description'">
            <span v-if="(record as PublicProjectMeta).public_description">
              {{ (record as PublicProjectMeta).public_description }}
            </span>
            <span v-else class="muted">—</span>
          </template>
          <template v-else-if="column.key === 'published_at'">
            <span v-if="(record as PublicProjectMeta).published_at">
              {{ fmtRelative((record as PublicProjectMeta).published_at) }}
            </span>
            <span v-else class="muted">—</span>
          </template>
          <template v-else-if="column.key === 'actions'">
            <a-popconfirm
              :title="t('page_admin_public_projects.revoke_confirm')"
              :ok-text="t('page_admin_public_projects.revoke_ok')"
              :cancel-text="t('common.cancel')"
              ok-type="danger"
              @confirm="doRevoke(record as PublicProjectMeta)"
            >
              <a-button size="small" danger>
                <template #icon><StopOutlined /></template>
                {{ t('page_admin_public_projects.btn_revoke') }}
              </a-button>
            </a-popconfirm>
          </template>
        </template>
        <template #emptyText>
          <div class="muted empty-wrap" style="padding: 24px 0">
            {{ t('page_admin_public_projects.empty') }}
          </div>
        </template>
      </a-table>
    </a-card>
  </div>
</template>
