<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import {
  ReloadOutlined,
  AppstoreOutlined,
  UnorderedListOutlined,
  StarFilled,
  DeleteOutlined,
} from '@ant-design/icons-vue';
import { message } from 'ant-design-vue';
import { listProjects, getProject } from '../api/projects';
import { bulkDeleteProjects } from '../api/client';
import { useStarsStore } from '../store/stars';
import { useAuthStore } from '../store/auth';
import type { ProjectSummary } from '../types';
import ProjectCard from '../components/ProjectCard.vue';
import StarButton from '../components/StarButton.vue';
import EmptyState from '../components/EmptyState.vue';
import AnonymousCTA from '../components/AnonymousCTA.vue';
import { usePermissions } from '../composables/usePermissions';
import {
  cached,
  peek,
  invalidate,
  schedulePrefetch,
  cancelPrefetch,
  cacheKey,
  cacheTtl,
} from '../composables/useCache';
import { fmtRelative } from '../utils/format';

const props = defineProps<{
  /**
   * When `true` the page mounts in read-only mode (demo / public entry).
   * Hides star toggles and anything that would mutate server state.
   * When `undefined` we fall back to the auth store.
   */
  readOnly?: boolean;
}>();

const { t } = useI18n();
const router = useRouter();
const stars = useStarsStore();
const auth = useAuthStore();
const { canWrite, isAnonymous } = usePermissions(props.readOnly);

// Seed from the module-scope TTL cache so a revisit from ProjectDetail
// paints instantly — skeleton only flashes on a true first visit.
const cachedInitial = peek<ProjectSummary[]>(cacheKey.projects('mine'));
const items = ref<ProjectSummary[]>(cachedInitial ?? []);
const loading = ref(!cachedInitial);
const viewMode = ref<'grid' | 'table'>('grid');
const scope = ref<'mine' | 'shared' | 'all'>('mine');
const onlyStarred = ref(false);
const search = ref('');

// Demo projects are now resolved entirely server-side: signed-in users
// never see them, anonymous visitors reach them via /demo/*. No client
// filter / banner / opt-out switch needed.

async function fetchAll(force = false) {
  const key = cacheKey.projects(scope.value);
  // Paint cached rows immediately on scope switch so the grid doesn't
  // flash empty while we wait on the network.
  const seed = peek<ProjectSummary[]>(key);
  if (seed && !force) items.value = seed;
  loading.value = items.value.length === 0;
  try {
    if (force) invalidate(key);
    items.value = await cached(
      key,
      () => listProjects({ scope: scope.value }).then((r) => r ?? []),
      cacheTtl.projects,
    );
  } catch {
    // interceptor notified
  } finally {
    loading.value = false;
  }
}

/** Warm the project-detail cache when the user hovers a card. */
function onProjectHover(project: string) {
  schedulePrefetch(
    cacheKey.projectSummary(project),
    () => getProject(project),
    cacheTtl.summary,
  );
}
function onProjectHoverEnd(project: string) {
  cancelPrefetch(cacheKey.projectSummary(project));
}

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase();
  return items.value.filter((p) => {
    if (onlyStarred.value && !(p.is_starred || stars.isStarred('project', p.project))) return false;
    if (q && !p.project.toLowerCase().includes(q)) return false;
    return true;
  });
});

const sorted = computed(() => {
  const arr = [...filtered.value];
  arr.sort((a, b) => {
    const aStar = a.is_starred || stars.isStarred('project', a.project) ? 1 : 0;
    const bStar = b.is_starred || stars.isStarred('project', b.project) ? 1 : 0;
    if (aStar !== bStar) return bStar - aStar;
    const aT = a.last_event_at ? Date.parse(a.last_event_at) : 0;
    const bT = b.last_event_at ? Date.parse(b.last_event_at) : 0;
    return bT - aT;
  });
  return arr;
});

function onScopeChange(key: string | number) {
  scope.value = String(key) as 'mine' | 'shared' | 'all';
  void fetchAll();
}

function onRefreshClick() {
  void fetchAll(true);
}

// Bulk-delete state — admin only.
const selectedProjects = ref<string[]>([]);
const bulkDeleting = ref(false);

const rowSelection = computed(() => ({
  selectedRowKeys: selectedProjects.value,
  onChange: (keys: (string | number)[]) => {
    selectedProjects.value = keys.map(String);
  },
}));

async function runBulkDelete() {
  if (!selectedProjects.value.length || bulkDeleting.value) return;
  bulkDeleting.value = true;
  try {
    const ids = selectedProjects.value.slice();
    const res = await bulkDeleteProjects(ids);
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
    selectedProjects.value = [];
    void fetchAll(true);
  } catch {
    // interceptor notifies
  } finally {
    bulkDeleting.value = false;
  }
}

const columns = computed(() => [
  { title: '', key: 'star', width: 48 },
  { title: t('page_project_list.col_project'), key: 'project' },
  { title: t('page_project_list.col_running'), key: 'running', width: 100 },
  { title: t('page_project_list.col_total_batches'), key: 'total', width: 120 },
  { title: t('page_project_list.col_jobs'), key: 'jobs', width: 160 },
  { title: t('page_project_list.col_best_metric'), key: 'best', width: 160 },
  { title: t('page_project_list.col_last_event'), key: 'last', width: 160 },
]);

onMounted(() => {
  void stars.ensureLoaded();
  void fetchAll();
});
</script>

<template>
  <div class="page-container">
    <!-- Persistent CTA for anonymous visitors on /demo. -->
    <AnonymousCTA v-if="isAnonymous" />

    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap">
      <div style="font-size: 18px; font-weight: 600">{{ $t('page_project_list.title') }}</div>

      <a-tabs
        v-if="canWrite"
        :active-key="scope"
        style="margin-left: 16px"
        @change="onScopeChange"
      >
        <a-tab-pane key="mine" :tab="$t('page_project_list.scope_mine')" />
        <a-tab-pane key="shared" :tab="$t('page_project_list.scope_shared')" />
        <a-tab-pane v-if="auth.isAdmin" key="all" :tab="$t('page_project_list.scope_all')" />
      </a-tabs>

      <span style="flex: 1" />

      <a-input-search
        v-model:value="search"
        :placeholder="$t('page_project_list.search_placeholder')"
        allow-clear
        style="width: 220px"
      />
      <!-- Starred filter is a per-user state; meaningless without an account. -->
      <a-tooltip v-if="canWrite" :title="$t('page_project_list.starred_tooltip')">
        <a-button :type="onlyStarred ? 'primary' : 'default'" @click="onlyStarred = !onlyStarred">
          <template #icon><StarFilled /></template>
          {{ $t('page_project_list.starred_btn') }}
        </a-button>
      </a-tooltip>
      <a-radio-group v-model:value="viewMode" button-style="solid">
        <a-radio-button value="grid"><AppstoreOutlined /></a-radio-button>
        <a-radio-button value="table"><UnorderedListOutlined /></a-radio-button>
      </a-radio-group>
      <a-button type="primary" :loading="loading" @click="onRefreshClick">
        <template #icon><ReloadOutlined /></template>
        {{ $t('common.refresh') }}
      </a-button>

      <a-popconfirm
        v-if="auth.isAdmin && selectedProjects.length > 0"
        :title="$t('common.bulk_delete_confirm', { n: selectedProjects.length })"
        :ok-text="$t('common.delete')"
        :cancel-text="$t('common.cancel')"
        ok-type="danger"
        @confirm="runBulkDelete"
      >
        <a-button danger :loading="bulkDeleting">
          <template #icon><DeleteOutlined /></template>
          {{ $t('common.bulk_delete_button', { n: selectedProjects.length }) }}
        </a-button>
      </a-popconfirm>
    </div>

    <!-- Skeleton on first paint — avoids the blank flash before the grid
         populates. Cache hits on revisit skip this entirely. -->
    <a-row
      v-if="loading && !sorted.length"
      :gutter="[16, 16]"
    >
      <a-col
        v-for="i in 6"
        :key="`skel-${i}`"
        :xs="24"
        :sm="12"
        :md="8"
        :lg="6"
      >
        <a-card size="small" :body-style="{ padding: '12px 14px' }">
          <a-skeleton active :title="{ width: '60%' }" :paragraph="{ rows: 2 }" />
        </a-card>
      </a-col>
    </a-row>

    <EmptyState
      v-else-if="!sorted.length && !loading"
      variant="empty_projects"
      :title="$t('page_project_list.empty')"
    />

    <a-row v-else-if="viewMode === 'grid'" :gutter="[16, 16]">
      <a-col
        v-for="p in sorted"
        :key="p.project"
        :xs="24"
        :sm="12"
        :md="8"
        :lg="6"
        @mouseenter="onProjectHover(p.project)"
        @mouseleave="onProjectHoverEnd(p.project)"
      >
        <ProjectCard :project="p" :read-only="!canWrite" />
      </a-col>
    </a-row>

    <a-table
      v-else
      :columns="columns"
      :data-source="sorted"
      :loading="loading"
      row-key="project"
      size="small"
      :pagination="{ pageSize: 30 }"
      :row-selection="auth.isAdmin ? rowSelection : undefined"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'star'">
          <StarButton
            v-if="canWrite"
            target-type="project"
            :target-id="(record as ProjectSummary).project"
            icon-only
          />
        </template>
        <template v-else-if="column.key === 'project'">
          <a
            href="#"
            @click.prevent="router.push(
              (canWrite ? '/projects/' : '/demo/projects/')
                + encodeURIComponent((record as ProjectSummary).project)
            )"
          >
            {{ (record as ProjectSummary).project }}
          </a>
        </template>
        <template v-else-if="column.key === 'running'">
          {{ (record as ProjectSummary).running_batches ?? 0 }}
        </template>
        <template v-else-if="column.key === 'total'">
          {{ (record as ProjectSummary).total_batches ?? 0 }}
        </template>
        <template v-else-if="column.key === 'jobs'">
          {{ (record as ProjectSummary).jobs_done ?? 0 }} /
          <span style="color: #ff7875">
            {{ (record as ProjectSummary).jobs_failed ?? 0 }}
          </span>
        </template>
        <template v-else-if="column.key === 'best'">
          <template v-if="(record as ProjectSummary).best_metric != null">
            {{ (record as ProjectSummary).best_metric_key ?? $t('common.metric') }}
            = {{ ((record as ProjectSummary).best_metric as number).toFixed(4) }}
          </template>
          <span v-else class="muted">—</span>
        </template>
        <template v-else-if="column.key === 'last'">
          <span class="muted" style="font-size: 12px">
            {{ fmtRelative((record as ProjectSummary).last_event_at) || '—' }}
          </span>
        </template>
      </template>
    </a-table>
  </div>
</template>
