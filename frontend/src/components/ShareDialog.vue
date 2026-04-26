<script setup lang="ts">
/**
 * Share dialog for a single batch. Three tabs:
 *   1. Users (batch)   — per-batch shares with grantees
 *   2. Users (project) — share whole project with a grantee (if `project` prop set)
 *   3. Public link     — anonymous read-only URL slugs
 *
 * Mounted on-demand; the owning page opens it with `v-model:open`.
 */
import { computed, ref, watch } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { CopyOutlined, DeleteOutlined, LinkOutlined, PlusOutlined } from '@ant-design/icons-vue';
import dayjs, { type Dayjs } from 'dayjs';
import {
  addBatchShare,
  addProjectShare,
  listBatchShares,
  listProjectShares,
  removeBatchShare,
  removeProjectShare,
} from '../api/shares';
import {
  createPublicShare,
  listBatchPublicShares,
  revokePublicShare,
} from '../api/public';
import type {
  BatchShare,
  ProjectShare,
  PublicShare,
  SharePermission,
} from '../types';
import { fmtTime, fmtRelative } from '../utils/format';

const { t } = useI18n();
const props = defineProps<{
  open: boolean;
  batchId: string;
  /** Used to filter project-level shares to just this project. */
  project?: string | null;
}>();

const emit = defineEmits<{
  (e: 'update:open', v: boolean): void;
  (e: 'changed'): void;
}>();

const activeTab = ref<'batch' | 'project' | 'public'>('batch');

// ---- Batch shares state ----
const batchShares = ref<BatchShare[]>([]);
const batchLoading = ref(false);
const batchForm = ref<{ grantee: string; permission: SharePermission }>({
  grantee: '',
  permission: 'viewer',
});
const batchSubmitting = ref(false);

// ---- Project shares state ----
const projectShares = ref<ProjectShare[]>([]);
const projectLoading = ref(false);
const projectForm = ref<{ grantee: string; permission: SharePermission }>({
  grantee: '',
  permission: 'viewer',
});
const projectSubmitting = ref(false);

// ---- Public shares state ----
const publicShares = ref<PublicShare[]>([]);
const publicLoading = ref(false);
const publicSubmitting = ref(false);
const publicExpiresAt = ref<Dayjs | undefined>(undefined);

// ---- Fetches ----

async function fetchBatchShares(): Promise<void> {
  batchLoading.value = true;
  try {
    batchShares.value = (await listBatchShares(props.batchId)) ?? [];
  } catch {
    // interceptor notified
  } finally {
    batchLoading.value = false;
  }
}

async function fetchProjectShares(): Promise<void> {
  if (!props.project) {
    projectShares.value = [];
    return;
  }
  projectLoading.value = true;
  try {
    const all = (await listProjectShares()) ?? [];
    projectShares.value = all.filter((p) => p.project === props.project);
  } catch {
    // interceptor notified
  } finally {
    projectLoading.value = false;
  }
}

async function fetchPublicShares(): Promise<void> {
  publicLoading.value = true;
  try {
    publicShares.value = (await listBatchPublicShares(props.batchId)) ?? [];
  } catch {
    publicShares.value = [];
  } finally {
    publicLoading.value = false;
  }
}

function fetchAllForTab(): void {
  if (activeTab.value === 'batch') void fetchBatchShares();
  else if (activeTab.value === 'project') void fetchProjectShares();
  else if (activeTab.value === 'public') void fetchPublicShares();
}

watch(
  () => props.open,
  (v) => {
    if (v) {
      activeTab.value = 'batch';
      void fetchBatchShares();
    }
  },
);

watch(activeTab, () => {
  if (props.open) fetchAllForTab();
});

// ---- Actions: batch shares ----

async function submitBatchShare(): Promise<void> {
  const grantee = batchForm.value.grantee.trim();
  if (!grantee) {
    notification.warning({ message: t('component_share_dialog.username_required'), duration: 2 });
    return;
  }
  batchSubmitting.value = true;
  try {
    await addBatchShare(props.batchId, {
      grantee_username: grantee,
      permission: batchForm.value.permission,
    });
    notification.success({ message: t('component_share_dialog.shared_with', { name: grantee }), duration: 2 });
    batchForm.value.grantee = '';
    await fetchBatchShares();
    emit('changed');
  } catch {
    // interceptor notified
  } finally {
    batchSubmitting.value = false;
  }
}

async function removeBatchShareRow(s: BatchShare): Promise<void> {
  try {
    await removeBatchShare(props.batchId, s.grantee_id);
    notification.success({ message: t('component_share_dialog.removed_access', { name: s.grantee_username }), duration: 2 });
    await fetchBatchShares();
    emit('changed');
  } catch {
    // interceptor notified
  }
}

// ---- Actions: project shares ----

async function submitProjectShare(): Promise<void> {
  if (!props.project) return;
  const grantee = projectForm.value.grantee.trim();
  if (!grantee) {
    notification.warning({ message: t('component_share_dialog.username_required'), duration: 2 });
    return;
  }
  projectSubmitting.value = true;
  try {
    await addProjectShare({
      project: props.project,
      grantee_username: grantee,
      permission: projectForm.value.permission,
    });
    notification.success({
      message: t('component_share_dialog.project_shared', { name: grantee }),
      duration: 2,
    });
    projectForm.value.grantee = '';
    await fetchProjectShares();
    emit('changed');
  } catch {
    // interceptor notified
  } finally {
    projectSubmitting.value = false;
  }
}

async function removeProjectShareRow(s: ProjectShare): Promise<void> {
  try {
    await removeProjectShare(s.project, s.grantee_id);
    notification.success({ message: t('component_share_dialog.removed_access', { name: s.grantee_username }), duration: 2 });
    await fetchProjectShares();
    emit('changed');
  } catch {
    // interceptor notified
  }
}

// ---- Actions: public shares ----

function fullPublicUrl(p: PublicShare): string {
  if (p.url?.startsWith('http')) return p.url;
  const base = typeof window !== 'undefined' ? window.location.origin : '';
  return `${base}/public/${p.slug}`;
}

async function generatePublicShare(): Promise<void> {
  publicSubmitting.value = true;
  try {
    const resp = await createPublicShare(props.batchId, {
      expires_at: publicExpiresAt.value ? publicExpiresAt.value.toISOString() : null,
    });
    notification.success({ message: t('component_share_dialog.public_created'), duration: 2 });
    publicExpiresAt.value = undefined;
    publicShares.value = [resp, ...publicShares.value];
    emit('changed');
  } catch {
    // interceptor notified
  } finally {
    publicSubmitting.value = false;
  }
}

async function copyPublicUrl(p: PublicShare): Promise<void> {
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
    notification.success({ message: t('component_share_dialog.link_copied'), duration: 2 });
  } catch {
    notification.error({ message: t('component_share_dialog.copy_failed'), duration: 3 });
  }
}

async function revokePublic(p: PublicShare): Promise<void> {
  try {
    await revokePublicShare(props.batchId, p.slug);
    notification.success({ message: t('component_share_dialog.public_revoked'), duration: 2 });
    publicShares.value = publicShares.value.filter((x) => x.slug !== p.slug);
    emit('changed');
  } catch {
    // interceptor notified
  }
}

// ---- Helpers ----

function permissionTag(p: SharePermission): { color: string; label: string } {
  if (p === 'editor') return { color: 'orange', label: 'editor' };
  return { color: 'blue', label: 'viewer' };
}

function disabledExpiryDate(current: Dayjs): boolean {
  return current && current.isBefore(dayjs().startOf('day'));
}

const batchColumns = computed(() => [
  { title: t('component_share_dialog.col_user'), dataIndex: 'grantee_username', key: 'grantee_username' },
  { title: t('component_share_dialog.col_permission'), key: 'permission', width: 120 },
  { title: t('component_share_dialog.col_since'), key: 'created_at', width: 170 },
  { title: '', key: 'actions', width: 90, fixed: 'right' as const },
]);

const projectColumns = computed(() => [
  { title: t('component_share_dialog.col_user'), dataIndex: 'grantee_username', key: 'grantee_username' },
  { title: t('component_share_dialog.col_permission'), key: 'permission', width: 120 },
  { title: t('component_share_dialog.col_since'), key: 'created_at', width: 170 },
  { title: '', key: 'actions', width: 90, fixed: 'right' as const },
]);

const publicColumns = computed(() => [
  { title: t('component_share_dialog.col_url'), key: 'url' },
  { title: t('component_share_dialog.col_created'), key: 'created_at', width: 170 },
  { title: t('component_share_dialog.col_expires'), key: 'expires_at', width: 170 },
  { title: t('component_share_dialog.col_views'), key: 'view_count', width: 80 },
  { title: '', key: 'actions', width: 120, fixed: 'right' as const },
]);

const hasProject = computed(() => !!props.project);
</script>

<template>
  <a-modal
    :open="open"
    :title="$t('component_share_dialog.title', { batchId })"
    :footer="null"
    width="680px"
    @update:open="(v: boolean) => emit('update:open', v)"
  >
    <a-tabs v-model:active-key="activeTab">
      <!-- Batch-level sharing -->
      <a-tab-pane key="batch" :tab="$t('component_share_dialog.tab_users_batch')">
        <div style="display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap">
          <a-input
            v-model:value="batchForm.grantee"
            :placeholder="$t('component_share_dialog.placeholder_username')"
            style="flex: 1; min-width: 180px"
            :disabled="batchSubmitting"
            @press-enter="submitBatchShare"
          />
          <a-select v-model:value="batchForm.permission" style="width: 120px">
            <a-select-option value="viewer">Viewer</a-select-option>
            <a-select-option value="editor">Editor</a-select-option>
          </a-select>
          <a-button type="primary" :loading="batchSubmitting" @click="submitBatchShare">
            <template #icon><PlusOutlined /></template>
            {{ $t('component_share_dialog.btn_share') }}
          </a-button>
        </div>

        <a-table
          :columns="batchColumns"
          :data-source="batchShares"
          :loading="batchLoading"
          row-key="grantee_id"
          size="small"
          :pagination="false"
          :scroll="{ x: 520 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'permission'">
              <a-tag :color="permissionTag((record as BatchShare).permission).color">
                {{ permissionTag((record as BatchShare).permission).label }}
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
                :title="`Revoke access for ${(record as BatchShare).grantee_username}?`"
                :ok-text="$t('component_share_dialog.popconfirm_ok')"
                ok-type="danger"
                @confirm="removeBatchShareRow(record as BatchShare)"
              >
                <a-button size="small" danger>
                  <template #icon><DeleteOutlined /></template>
                </a-button>
              </a-popconfirm>
            </template>
          </template>

          <template #emptyText>
            <div class="muted empty-wrap" style="padding: 24px">
              {{ $t('component_share_dialog.empty_batch_shares') }}
            </div>
          </template>
        </a-table>
      </a-tab-pane>

      <!-- Project-level sharing -->
      <a-tab-pane key="project" :tab="$t('component_share_dialog.tab_users_project')">
        <a-alert
          v-if="!hasProject"
          type="warning"
          show-icon
          :message="$t('component_share_dialog.no_project_msg')"
          :description="$t('component_share_dialog.no_project_desc')"
          style="margin-bottom: 12px"
        />
        <template v-else>
          <a-alert
            type="info"
            show-icon
            :message="$t('component_share_dialog.project_share_info')"
            :description="$t('component_share_dialog.project_share_desc')"
            style="margin-bottom: 12px"
          />
          <div style="display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap">
            <a-input
              v-model:value="projectForm.grantee"
              :placeholder="$t('component_share_dialog.placeholder_username')"
              style="flex: 1; min-width: 180px"
              :disabled="projectSubmitting"
              @press-enter="submitProjectShare"
            />
            <a-select v-model:value="projectForm.permission" style="width: 120px">
              <a-select-option value="viewer">Viewer</a-select-option>
              <a-select-option value="editor">Editor</a-select-option>
            </a-select>
            <a-button type="primary" :loading="projectSubmitting" @click="submitProjectShare">
              <template #icon><PlusOutlined /></template>
              {{ $t('component_share_dialog.btn_share_project') }}
            </a-button>
          </div>

          <a-table
            :columns="projectColumns"
            :data-source="projectShares"
            :loading="projectLoading"
            row-key="grantee_id"
            size="small"
            :pagination="false"
            :scroll="{ x: 520 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'permission'">
                <a-tag :color="permissionTag((record as ProjectShare).permission).color">
                  {{ permissionTag((record as ProjectShare).permission).label }}
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
                  :title="`Revoke access for ${(record as ProjectShare).grantee_username}?`"
                  :ok-text="$t('component_share_dialog.popconfirm_ok')"
                  ok-type="danger"
                  @confirm="removeProjectShareRow(record as ProjectShare)"
                >
                  <a-button size="small" danger>
                    <template #icon><DeleteOutlined /></template>
                  </a-button>
                </a-popconfirm>
              </template>
            </template>

            <template #emptyText>
              <div class="muted empty-wrap" style="padding: 24px">
                {{ $t('component_share_dialog.empty_project_shares') }}
              </div>
            </template>
          </a-table>
        </template>
      </a-tab-pane>

      <!-- Public link -->
      <a-tab-pane key="public" :tab="$t('component_share_dialog.tab_public_link')">
        <a-alert
          type="info"
          show-icon
          :message="$t('component_share_dialog.public_link_msg')"
          :description="$t('component_share_dialog.public_link_desc')"
          style="margin-bottom: 12px"
        />
        <div
          style="display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center"
        >
          <a-date-picker
            v-model:value="publicExpiresAt"
            :placeholder="$t('component_share_dialog.placeholder_expires')"
            :disabled-date="disabledExpiryDate"
            style="min-width: 200px"
          />
          <a-button type="primary" :loading="publicSubmitting" @click="generatePublicShare">
            <template #icon><LinkOutlined /></template>
            {{ $t('component_share_dialog.btn_generate') }}
          </a-button>
        </div>

        <a-table
          :columns="publicColumns"
          :data-source="publicShares"
          :loading="publicLoading"
          row-key="slug"
          size="small"
          :pagination="false"
          :scroll="{ x: 680 }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'url'">
              <code
                style="font-size: 11px; word-break: break-all; display: inline-block; max-width: 280px"
              >
                {{ fullPublicUrl(record as PublicShare) }}
              </code>
            </template>
            <template v-else-if="column.key === 'created_at'">
              <span v-if="(record as PublicShare).created_at">
                {{ fmtRelative((record as PublicShare).created_at) }}
              </span>
              <span v-else class="muted">—</span>
            </template>
            <template v-else-if="column.key === 'expires_at'">
              <span v-if="(record as PublicShare).expires_at">
                {{ fmtTime((record as PublicShare).expires_at) }}
              </span>
              <a-tag v-else color="default">{{ $t('component_share_dialog.never_expires') }}</a-tag>
            </template>
            <template v-else-if="column.key === 'view_count'">
              {{ (record as PublicShare).view_count ?? 0 }}
            </template>
            <template v-else-if="column.key === 'actions'">
              <a-space size="small">
                <a-tooltip :title="$t('component_share_dialog.tooltip_copy_link')">
                  <a-button size="small" @click="copyPublicUrl(record as PublicShare)">
                    <template #icon><CopyOutlined /></template>
                  </a-button>
                </a-tooltip>
                <a-popconfirm
                  :title="$t('component_share_dialog.popconfirm_revoke')"
                  :description="$t('component_share_dialog.popconfirm_revoke_desc')"
                  :ok-text="$t('component_share_dialog.popconfirm_ok')"
                  ok-type="danger"
                  @confirm="revokePublic(record as PublicShare)"
                >
                  <a-button size="small" danger>
                    <template #icon><DeleteOutlined /></template>
                  </a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>

          <template #emptyText>
            <div class="muted empty-wrap" style="padding: 24px">
              {{ $t('component_share_dialog.empty_public_links') }}
            </div>
          </template>
        </a-table>
      </a-tab-pane>
    </a-tabs>
  </a-modal>
</template>
