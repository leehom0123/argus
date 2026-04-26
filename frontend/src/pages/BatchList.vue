<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed, watch } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import { ReloadOutlined, AppstoreOutlined, UnorderedListOutlined, DeleteOutlined } from '@ant-design/icons-vue';
import { message } from 'ant-design-vue';
import { useBatchesStore } from '../store/batches';
import { useAppStore } from '../store/app';
import { useAuthStore } from '../store/auth';
import StatusTag from '../components/StatusTag.vue';
import ProgressInline from '../components/ProgressInline.vue';
import BatchCompactCard from '../components/BatchCompactCard.vue';
import EmptyState from '../components/EmptyState.vue';
import { getBatch, bulkDeleteBatches } from '../api/client';
import { compactItemToBatchCompactData } from '../composables/useBatchCompactData';
import { schedulePrefetch, cancelPrefetch, cacheKey, cacheTtl } from '../composables/useCache';
import { fmtTime, durationBetween, fmtRelative } from '../utils/format';
import type { Batch, BatchScope, BatchStatus } from '../types';

const { t } = useI18n();
const store = useBatchesStore();
const appStore = useAppStore();
const auth = useAuthStore();
const router = useRouter();

const autoRefresh = ref(true);
let timer: number | null = null;

// View-mode toggle: 'compact' (default) or 'expanded' (table). Persisted to localStorage.
const LS_VIEW_MODE_KEY = 'argus.batch-list.view-mode';
const viewMode = ref<'compact' | 'expanded'>(
  (localStorage.getItem(LS_VIEW_MODE_KEY) as 'compact' | 'expanded') ?? 'compact',
);
watch(viewMode, (v) => localStorage.setItem(LS_VIEW_MODE_KEY, v));

// Scope tab state. Default to `mine` — matches Dashboard IA §16.2.
// Initialize from the current filter so navigating back to the page preserves choice.
const scope = ref<BatchScope>((store.filters.scope as BatchScope | undefined) ?? 'mine');

const statusOptions: BatchStatus[] = ['pending', 'running', 'done', 'failed', 'partial'];

const columns = computed(() => [
  {
    title: t('page_batch_list.col_batch_id'),
    dataIndex: 'id',
    key: 'id',
    fixed: 'left' as const,
    width: 240,
    sorter: (a: Batch, b: Batch) => a.id.localeCompare(b.id),
  },
  { title: t('page_batch_list.col_project'), dataIndex: 'project', key: 'project', width: 180 },
  { title: t('page_batch_list.col_user'), dataIndex: 'user', key: 'user', width: 120 },
  { title: t('page_batch_list.col_type'), dataIndex: 'experiment_type', key: 'experiment_type', width: 120 },
  { title: t('page_batch_list.col_status'), key: 'status', width: 110 },
  { title: t('page_batch_list.col_progress'), key: 'progress', width: 220 },
  {
    title: t('page_batch_list.col_start'),
    key: 'start_time',
    width: 200,
    sorter: (a: Batch, b: Batch) => (a.start_time ?? '').localeCompare(b.start_time ?? ''),
    defaultSortOrder: 'descend' as const,
  },
  { title: t('page_batch_list.col_duration'), key: 'duration', width: 120 },
  { title: t('page_batch_list.col_host'), dataIndex: 'host', key: 'host', width: 160 },
]);

const filtered = computed(() => store.filtered);

function startTimer() {
  stopTimer();
  if (!autoRefresh.value) return;
  const interval = Math.max(5, appStore.autoRefreshSec) * 1000;
  // In compact view the page depends on the bulk endpoint payload (jobs +
  // epochs + resources), so auto-refresh must call fetchCompact too.
  timer = window.setInterval(() => {
    if (viewMode.value === 'compact') {
      void store.fetchCompact();
    } else {
      void store.fetch();
    }
  }, interval);
}

function stopTimer() {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

function refresh() {
  if (viewMode.value === 'compact') {
    void store.fetchCompact();
  } else {
    void store.fetch();
  }
}

// Swapping view modes mid-session may leave the wrong data loaded; refetch
// on toggle so the newly-visible surface is populated immediately.
watch(viewMode, () => {
  refresh();
});

function openBatch(b: Batch) {
  router.push(`/batches/${encodeURIComponent(b.id)}`);
}

function onBatchHover(id: string) {
  schedulePrefetch(
    cacheKey.batchSummary(id),
    () => getBatch(id),
    cacheTtl.summary,
  );
}
function onBatchHoverEnd(id: string) {
  cancelPrefetch(cacheKey.batchSummary(id));
}

function onRangeChange(_dates: unknown, dateStrings: [string, string] | string[]) {
  const [from, to] = (dateStrings ?? []) as string[];
  store.setFilter('since', from || undefined);
  store.setFilter('until', to || undefined);
  refresh();
}

function onScopeChange(key: string | number) {
  const s = String(key) as BatchScope;
  scope.value = s;
  store.setFilter('scope', s);
  refresh();
}

const emptyMsg = computed(() => {
  switch (scope.value) {
    case 'shared':
      return t('page_batch_list.empty_shared');
    case 'all':
      return t('page_batch_list.empty_all');
    default:
      return t('page_batch_list.empty_mine');
  }
});

const emptyHint = computed(() => {
  switch (scope.value) {
    case 'shared':
      return t('page_batch_list.empty_shared_hint');
    case 'all':
      return t('page_batch_list.empty_all_hint');
    default:
      return t('page_batch_list.empty_mine_hint');
  }
});

/**
 * Map the batch-list scope to the corresponding hint-catalog variant
 * (#30). 'shared' → ``empty_shared``; everything else → ``empty_batches``.
 * EmptyState prefers the backend catalog over the local hint; callers can
 * still pass ``:hint="emptyHint"`` to override either one.
 */
const emptyVariant = computed<'empty_shared' | 'empty_batches'>(() =>
  scope.value === 'shared' ? 'empty_shared' : 'empty_batches',
);

// Bulk-delete state. Selection survives view-mode toggle.
const selectedIds = ref<string[]>([]);
const bulkDeleting = ref(false);

const rowSelection = computed(() => ({
  selectedRowKeys: selectedIds.value,
  onChange: (keys: (string | number)[]) => {
    selectedIds.value = keys.map(String);
  },
}));

function toggleCardSelected(id: string, on: boolean) {
  const idx = selectedIds.value.indexOf(id);
  if (on && idx === -1) selectedIds.value = [...selectedIds.value, id];
  else if (!on && idx !== -1) {
    const next = selectedIds.value.slice();
    next.splice(idx, 1);
    selectedIds.value = next;
  }
}

async function runBulkDelete() {
  if (!selectedIds.value.length || bulkDeleting.value) return;
  bulkDeleting.value = true;
  try {
    const ids = selectedIds.value.slice();
    const res = await bulkDeleteBatches(ids);
    if (res.skipped.length === 0) {
      message.success(t('common.bulk_delete_success', { n: res.deleted.length }));
    } else {
      message.warning(
        t('common.bulk_delete_partial', {
          deleted: res.deleted.length,
          total: ids.length,
        }),
      );
    }
    selectedIds.value = [];
    refresh();
  } catch {
    // interceptor notifies
  } finally {
    bulkDeleting.value = false;
  }
}

watch(autoRefresh, () => startTimer());
watch(() => appStore.autoRefreshSec, () => startTimer());

onMounted(() => {
  // Seed the filter in case we came from a page that didn't route-populate it.
  store.setFilter('scope', scope.value);
  refresh();
  startTimer();
});

onUnmounted(stopTimer);
</script>

<template>
  <div class="page-container">
    <a-tabs :active-key="scope" style="margin-bottom: 8px" @change="onScopeChange">
      <a-tab-pane key="mine" :tab="$t('page_batch_list.tab_mine')" />
      <a-tab-pane key="shared" :tab="$t('page_batch_list.tab_shared')" />
      <a-tab-pane v-if="auth.isAdmin" key="all" :tab="$t('page_batch_list.tab_all')" />
    </a-tabs>

    <div class="filter-bar">
      <a-select
        v-model:value="store.filters.experiment_type"
        :placeholder="$t('page_batch_list.filter_type')"
        allow-clear
        style="width: 160px"
        :options="store.experimentTypes.map((t) => ({ value: t, label: t }))"
      />
      <a-select
        v-model:value="store.filters.project"
        :placeholder="$t('page_batch_list.filter_project')"
        allow-clear
        style="width: 200px"
        :options="store.projects.map((p) => ({ value: p, label: p }))"
        @change="refresh"
      />
      <a-select
        v-model:value="store.filters.status"
        :placeholder="$t('page_batch_list.filter_status')"
        allow-clear
        style="width: 140px"
        :options="statusOptions.map((s) => ({ value: s, label: s }))"
        @change="refresh"
      />
      <a-input-search
        v-model:value="store.filters.q"
        :placeholder="$t('page_batch_list.search_placeholder')"
        style="width: 260px"
        allow-clear
      />
      <a-range-picker :show-time="false" @change="onRangeChange" />

      <span style="flex: 1" />

      <a-tooltip :title="$t('page_batch_list.auto_refresh')">
        <a-switch
          v-model:checked="autoRefresh"
          :checked-children="$t('page_dashboard.auto_on')"
          :un-checked-children="$t('page_dashboard.auto_off')"
        />
      </a-tooltip>
      <a-input-number
        v-model:value="appStore.autoRefreshSec"
        :min="5"
        :max="600"
        :step="5"
        style="width: 90px"
        addon-after="s"
      />
      <a-button type="primary" :loading="store.loading" @click="refresh">
        <template #icon><ReloadOutlined /></template>
        {{ $t('common.refresh') }}
      </a-button>

      <a-popconfirm
        v-if="selectedIds.length > 0"
        :title="$t('common.bulk_delete_confirm', { n: selectedIds.length })"
        :ok-text="$t('common.delete')"
        :cancel-text="$t('common.cancel')"
        ok-type="danger"
        @confirm="runBulkDelete"
      >
        <a-button danger :loading="bulkDeleting">
          <template #icon><DeleteOutlined /></template>
          {{ $t('common.bulk_delete_button', { n: selectedIds.length }) }}
        </a-button>
      </a-popconfirm>

      <!-- View mode toggle -->
      <a-tooltip :title="$t('page_batch_list_compact.view_compact')">
        <a-button
          size="small"
          :type="viewMode === 'compact' ? 'primary' : 'default'"
          @click="viewMode = 'compact'"
        >
          <template #icon><AppstoreOutlined /></template>
        </a-button>
      </a-tooltip>
      <a-tooltip :title="$t('page_batch_list_compact.view_expanded')">
        <a-button
          size="small"
          :type="viewMode === 'expanded' ? 'primary' : 'default'"
          @click="viewMode = 'expanded'"
        >
          <template #icon><UnorderedListOutlined /></template>
        </a-button>
      </a-tooltip>
    </div>

    <!-- Compact card grid — responsive columns matching Dashboard's Running
         batches section so users get consistent layout across pages. 1 col on
         phones, 2 on tablets, 3 on wide screens. -->
    <div v-if="viewMode === 'compact'">
      <template v-if="store.loading && !filtered.length">
        <a-row :gutter="[12, 12]">
          <a-col
            v-for="i in 6"
            :key="`skel-${i}`"
            :xs="24"
            :sm="12"
            :xl="8"
          >
            <a-card size="small" :body-style="{ padding: '12px 14px' }">
              <a-skeleton active :title="{ width: '40%' }" :paragraph="{ rows: 2 }" />
            </a-card>
          </a-col>
        </a-row>
      </template>
      <EmptyState
        v-else-if="!filtered.length && !store.loading"
        :variant="emptyVariant"
        :title="emptyMsg"
        :hint="emptyHint"
      >
        <a-button @click="refresh">{{ $t('page_batch_list.retry') }}</a-button>
      </EmptyState>
      <a-row v-else :gutter="[12, 12]">
        <a-col
          v-for="b in filtered"
          :key="b.id"
          :xs="24"
          :sm="12"
          :xl="8"
          @mouseenter="onBatchHover(b.id)"
          @mouseleave="onBatchHoverEnd(b.id)"
        >
          <div style="position: relative">
            <a-checkbox
              :checked="selectedIds.includes(b.id)"
              style="position: absolute; top: 6px; right: 6px; z-index: 2; background: rgba(0,0,0,0.45); padding: 2px 4px; border-radius: 4px"
              @change="(e: any) => toggleCardSelected(b.id, !!e.target.checked)"
            />
            <!-- Prefer the bulk-fetched payload (1 call for N batches). Fall
                 back to per-card internal fetch only if the store doesn't
                 have this batch id in its compact map (shouldn't happen in
                 practice, but keeps the card resilient). -->
            <BatchCompactCard
              v-if="store.compactByBatchId[b.id]"
              :compact-data="compactItemToBatchCompactData(store.compactByBatchId[b.id])"
              :refresh-key="store.lastFetchedAt"
            />
            <BatchCompactCard v-else :batch-id="b.id" />
          </div>
        </a-col>
      </a-row>
    </div>

    <a-table
      v-else
      :columns="columns"
      :data-source="filtered"
      :loading="store.loading"
      row-key="id"
      size="small"
      :scroll="{ x: 1400 }"
      :pagination="{ pageSize: 20, showSizeChanger: true }"
      :row-selection="rowSelection"
      :custom-row="(record: Batch) => ({
        onMouseenter: () => onBatchHover(record.id),
        onMouseleave: () => onBatchHoverEnd(record.id),
      })"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'id'">
          <a @click.prevent="openBatch(record as Batch)" href="#">
            {{ (record as Batch).id }}
          </a>
        </template>
        <template v-else-if="column.key === 'status'">
          <StatusTag :status="(record as Batch).status" />
        </template>
        <template v-else-if="column.key === 'progress'">
          <ProgressInline
            :done="(record as Batch).n_done"
            :total="(record as Batch).n_total"
            :failed="(record as Batch).n_failed"
          />
        </template>
        <template v-else-if="column.key === 'start_time'">
          <div style="line-height: 1.2">
            <div>{{ fmtTime((record as Batch).start_time) }}</div>
            <div class="muted" style="font-size: 11px">
              {{ fmtRelative((record as Batch).start_time) }}
            </div>
          </div>
        </template>
        <template v-else-if="column.key === 'duration'">
          {{ durationBetween((record as Batch).start_time, (record as Batch).end_time) }}
        </template>
      </template>

      <template #emptyText>
        <EmptyState :variant="emptyVariant" :title="emptyMsg" :hint="emptyHint">
          <a-button @click="refresh">{{ $t('page_batch_list.retry') }}</a-button>
        </EmptyState>
      </template>
    </a-table>
  </div>
</template>
