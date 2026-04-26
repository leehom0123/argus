<script setup lang="ts">
/**
 * JobsList.vue (#118) — global cross-batch jobs table.
 *
 * Renders every job the caller can see across batches/hosts/projects in one
 * paginated table. Filters (status, project, host, batch_id, since) sync
 * to the URL query string so deep-links from the Dashboard tiles
 * ("Jobs running" → ``/jobs?status=running``) land on a pre-filtered view.
 *
 * Visibility is enforced server-side: the response only contains rows the
 * caller's batches resolve to via VisibilityResolver. There's no extra
 * client-side RBAC check here.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import { ReloadOutlined } from '@ant-design/icons-vue';
import EmptyState from '../components/EmptyState.vue';
import { getStatusColor } from '../composables/useStatusColor';
import { listJobsGlobal } from '../api/client';
import { useAppStore } from '../store/app';
import { durationBetween, fmtRelative, fmtTime } from '../utils/format';
import type { GlobalJobItem, JobStatus } from '../types';

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const appStore = useAppStore();

// ---------------------------------------------------------------------------
// Filters — initialized from the URL so dashboard deep-links land filtered.
// ---------------------------------------------------------------------------

const statusOptions: JobStatus[] = ['pending', 'running', 'done', 'failed', 'skipped'];

const status = ref<string | undefined>(
  typeof route.query.status === 'string' ? route.query.status : undefined,
);
const project = ref<string | undefined>(
  typeof route.query.project === 'string' ? route.query.project : undefined,
);
const host = ref<string | undefined>(
  typeof route.query.host === 'string' ? route.query.host : undefined,
);
const batchId = ref<string | undefined>(
  typeof route.query.batch_id === 'string' ? route.query.batch_id : undefined,
);
const since = ref<string | undefined>(
  typeof route.query.since === 'string' ? route.query.since : undefined,
);
const page = ref<number>(Number(route.query.page ?? 1) || 1);
const pageSize = ref<number>(Number(route.query.page_size ?? 50) || 50);

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

const items = ref<GlobalJobItem[]>([]);
const total = ref<number>(0);
const loading = ref<boolean>(false);
const autoRefresh = ref<boolean>(true);
let timer: number | null = null;

async function fetchPage() {
  loading.value = true;
  try {
    const res = await listJobsGlobal({
      status: status.value,
      project: project.value,
      host: host.value,
      batch_id: batchId.value,
      since: since.value,
      page: page.value,
      page_size: pageSize.value,
    });
    // Defensive null-check: tests using ``mockResolvedValueOnce`` return
    // undefined on subsequent calls (e.g. when an auto-refresh tick or a
    // route-watcher re-fires after the single mock is consumed). The page
    // should keep its last-good state rather than throw and bubble up an
    // unhandled rejection.
    if (res) {
      items.value = res.items;
      total.value = res.total;
    }
  } finally {
    loading.value = false;
  }
}

function syncQuery() {
  // Build the next query without ``undefined`` keys so the URL stays clean.
  const q: Record<string, string> = {};
  if (status.value) q.status = status.value;
  if (project.value) q.project = project.value;
  if (host.value) q.host = host.value;
  if (batchId.value) q.batch_id = batchId.value;
  if (since.value) q.since = since.value;
  if (page.value > 1) q.page = String(page.value);
  if (pageSize.value !== 50) q.page_size = String(pageSize.value);
  router.replace({ path: '/jobs', query: q });
}

function applyFilters() {
  // Any filter change resets to page 1 — otherwise the user lands on a page
  // that may not exist in the new result set.
  page.value = 1;
  syncQuery();
  void fetchPage();
}

function refresh() {
  void fetchPage();
}

// Distinct dropdown options derived from current page (best-effort hint;
// backend has no ``/jobs/projects`` summary route yet — typing free-text
// in the input is the canonical way for filters not in the result set).
const projectOptions = computed(() => {
  const set = new Set<string>();
  for (const it of items.value) if (it.project) set.add(it.project);
  return Array.from(set).sort().map((v) => ({ value: v, label: v }));
});
const hostOptions = computed(() => {
  const set = new Set<string>();
  for (const it of items.value) if (it.host) set.add(it.host);
  return Array.from(set).sort().map((v) => ({ value: v, label: v }));
});

// Auto-refresh — same cadence as Dashboard / BatchList.
function startTimer() {
  stopTimer();
  if (!autoRefresh.value) return;
  const interval = Math.max(5, appStore.autoRefreshSec) * 1000;
  timer = window.setInterval(() => void fetchPage(), interval);
}
function stopTimer() {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

// ---------------------------------------------------------------------------
// Table columns
// ---------------------------------------------------------------------------

const columns = computed(() => [
  { title: t('page_jobs_list.col_job_id'), dataIndex: ['job', 'id'], key: 'id', width: 180 },
  { title: t('page_jobs_list.col_project'), key: 'project', width: 140 },
  { title: t('page_jobs_list.col_batch'), key: 'batch', width: 200 },
  { title: t('page_jobs_list.col_model'), key: 'model', width: 130 },
  { title: t('page_jobs_list.col_dataset'), key: 'dataset', width: 130 },
  { title: t('page_jobs_list.col_host'), key: 'host', width: 140 },
  { title: t('page_jobs_list.col_status'), key: 'status', width: 110 },
  { title: t('page_jobs_list.col_start'), key: 'start_time', width: 200 },
  { title: t('page_jobs_list.col_elapsed'), key: 'elapsed', width: 110 },
]);

function openJob(it: GlobalJobItem) {
  router.push(
    `/batches/${encodeURIComponent(it.job.batch_id)}/jobs/${encodeURIComponent(it.job.id)}`,
  );
}

function openBatch(it: GlobalJobItem) {
  router.push(`/batches/${encodeURIComponent(it.job.batch_id)}`);
}

// React to URL changes (back/forward navigation, dashboard tile clicks).
watch(
  () => route.query,
  (q) => {
    status.value = typeof q.status === 'string' ? q.status : undefined;
    project.value = typeof q.project === 'string' ? q.project : undefined;
    host.value = typeof q.host === 'string' ? q.host : undefined;
    batchId.value = typeof q.batch_id === 'string' ? q.batch_id : undefined;
    since.value = typeof q.since === 'string' ? q.since : undefined;
    page.value = Number(q.page ?? 1) || 1;
    pageSize.value = Number(q.page_size ?? 50) || 50;
    void fetchPage();
  },
);

watch(autoRefresh, () => startTimer());
watch(() => appStore.autoRefreshSec, () => startTimer());

onMounted(() => {
  void fetchPage();
  startTimer();
});

onUnmounted(stopTimer);

const pagination = computed(() => ({
  current: page.value,
  pageSize: pageSize.value,
  total: total.value,
  showSizeChanger: true,
  pageSizeOptions: ['20', '50', '100', '200'],
  showTotal: (n: number) => t('page_jobs_list.pagination_total', { n }),
}));

function onTableChange(p: { current?: number; pageSize?: number }) {
  const nextPage = p.current ?? 1;
  const nextSize = p.pageSize ?? 50;
  if (nextPage !== page.value || nextSize !== pageSize.value) {
    page.value = nextPage;
    pageSize.value = nextSize;
    syncQuery();
    void fetchPage();
  }
}
</script>

<template>
  <div class="page-container">
    <div class="filter-bar" data-testid="jobs-filter-bar">
      <a-select
        v-model:value="status"
        :placeholder="$t('page_jobs_list.filter_status')"
        allow-clear
        style="width: 140px"
        :options="statusOptions.map((s) => ({ value: s, label: s }))"
        @change="applyFilters"
      />
      <a-select
        v-model:value="project"
        :placeholder="$t('page_jobs_list.filter_project')"
        allow-clear
        show-search
        style="width: 200px"
        :options="projectOptions"
        @change="applyFilters"
      />
      <a-select
        v-model:value="host"
        :placeholder="$t('page_jobs_list.filter_host')"
        allow-clear
        show-search
        style="width: 180px"
        :options="hostOptions"
        @change="applyFilters"
      />
      <a-input
        v-model:value="batchId"
        :placeholder="$t('page_jobs_list.filter_batch_id')"
        allow-clear
        style="width: 220px"
        @pressEnter="applyFilters"
      />
      <a-input
        v-model:value="since"
        :placeholder="$t('page_jobs_list.filter_since')"
        allow-clear
        style="width: 140px"
        @pressEnter="applyFilters"
      />
      <a-button @click="applyFilters">
        {{ $t('page_jobs_list.apply') }}
      </a-button>

      <span style="flex: 1" />

      <a-tooltip :title="$t('page_jobs_list.auto_refresh')">
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
      <a-button type="primary" :loading="loading" @click="refresh">
        <template #icon><ReloadOutlined /></template>
        {{ $t('common.refresh') }}
      </a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="items"
      :loading="loading"
      :row-key="(record: GlobalJobItem) => `${record.job.batch_id}/${record.job.id}`"
      size="small"
      :scroll="{ x: 1500 }"
      :pagination="pagination"
      data-testid="jobs-table"
      @change="onTableChange"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'id'">
          <a @click.prevent="openJob(record as GlobalJobItem)" href="#">
            {{ (record as GlobalJobItem).job.id }}
          </a>
        </template>
        <template v-else-if="column.key === 'project'">
          {{ (record as GlobalJobItem).project }}
        </template>
        <template v-else-if="column.key === 'batch'">
          <a @click.prevent="openBatch(record as GlobalJobItem)" href="#">
            {{ (record as GlobalJobItem).batch_name ?? (record as GlobalJobItem).job.batch_id }}
          </a>
        </template>
        <template v-else-if="column.key === 'model'">
          {{ (record as GlobalJobItem).job.model ?? '—' }}
        </template>
        <template v-else-if="column.key === 'dataset'">
          {{ (record as GlobalJobItem).job.dataset ?? '—' }}
        </template>
        <template v-else-if="column.key === 'host'">
          {{ (record as GlobalJobItem).host ?? '—' }}
        </template>
        <template v-else-if="column.key === 'status'">
          <!--
            Use ``getStatusColor('job', ...)`` directly so ``is_idle_flagged``
            forces the canonical ``stalled`` bucket per #125. StatusTag.vue
            doesn't accept idle-flag context, which would silently drop
            idle-flagged jobs back to their underlying (often "running")
            colour. The label stays as the raw status — the yellow tag +
            screen-reader aria signal the stalled state.
          -->
          <a-tag
            :color="getStatusColor('job', (record as GlobalJobItem).job.status ?? '', { isIdleFlagged: !!(record as GlobalJobItem).job.is_idle_flagged }).tag"
            :aria-label="getStatusColor('job', (record as GlobalJobItem).job.status ?? '', { isIdleFlagged: !!(record as GlobalJobItem).job.is_idle_flagged }).aria"
          >
            {{ ((record as GlobalJobItem).job.status ?? '').toUpperCase() || '—' }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'start_time'">
          <div style="line-height: 1.2">
            <div>{{ fmtTime((record as GlobalJobItem).job.start_time) }}</div>
            <div class="muted" style="font-size: 11px">
              {{ fmtRelative((record as GlobalJobItem).job.start_time) }}
            </div>
          </div>
        </template>
        <template v-else-if="column.key === 'elapsed'">
          {{
            durationBetween(
              (record as GlobalJobItem).job.start_time,
              (record as GlobalJobItem).job.end_time,
            )
          }}
        </template>
      </template>

      <template #emptyText>
        <EmptyState
          variant="empty_batches"
          :title="$t('page_jobs_list.empty_title')"
          :hint="$t('page_jobs_list.empty_hint')"
        >
          <a-button @click="refresh">{{ $t('page_jobs_list.retry') }}</a-button>
        </EmptyState>
      </template>
    </a-table>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
</style>
