<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import {
  ReloadOutlined,
  KeyOutlined,
  ShareAltOutlined,
  ThunderboltOutlined,
  NotificationOutlined,
} from '@ant-design/icons-vue';
import { useDashboardStore } from '../store/dashboard';
import { useAuthStore } from '../store/auth';
import { useStarsStore } from '../store/stars';
import MetricTile from '../components/MetricTile.vue';
import ProjectCard from '../components/ProjectCard.vue';
import ActivityFeed from '../components/ActivityFeed.vue';
import HostCard from '../components/HostCard.vue';
import BatchCompactCard from '../components/BatchCompactCard.vue';
import HostCapacityChip from '../components/HostCapacityChip.vue';
import { getBatchesCompact, getBatch, type BatchCompactItem } from '../api/client';
import { compactItemToBatchCompactData } from '../composables/useBatchCompactData';
import { getProject } from '../api/projects';
import { schedulePrefetch, cancelPrefetch, cacheKey, cacheTtl } from '../composables/useCache';
import { fmtRelative } from '../utils/format';
import type { ProjectSummary, HostSummary } from '../types';

// GpuHoursTile pulls in an echarts bundle that the Dashboard doesn't need
// for its counters / project grid. Defer so the initial paint doesn't
// block on the chart code.
const GpuHoursTile = defineAsyncComponent(() => import('../components/GpuHoursTile.vue'));

const { t } = useI18n();
const dash = useDashboardStore();
const stars = useStarsStore();
const auth = useAuthStore();
const router = useRouter();

// Running batch compact items for the bottom grid. Fetched via the bulk
// ``/batches/compact`` endpoint so each card doesn't need to fan out to
// /batches/{id} + /jobs + /epochs/latest + /resources on its own.
const runningBatches = ref<BatchCompactItem[]>([]);

async function fetchRunningBatches() {
  try {
    const payload = await getBatchesCompact({
      status: 'running',
      scope: 'mine',
      limit: 10,
      resource_limit: 20,
    });
    runningBatches.value = payload.batches ?? [];
  } catch {
    runningBatches.value = [];
  }
}

// Filter for project grid (requirements §16.2 "Mine / Shared / All").
const projectFilter = ref<'mine' | 'shared' | 'all'>('mine');

function onScopeChange(key: string | number) {
  const s = String(key) as 'mine' | 'shared' | 'all';
  projectFilter.value = s;
  dash.setScope(s);
}

const orderedProjects = computed<ProjectSummary[]>(() => {
  const items = [...(dash.projects ?? [])];
  // Starred first, then by last_event_at desc.
  items.sort((a, b) => {
    const aStar = a.is_starred || stars.isStarred('project', a.project) ? 1 : 0;
    const bStar = b.is_starred || stars.isStarred('project', b.project) ? 1 : 0;
    if (aStar !== bStar) return bStar - aStar;
    const aT = a.last_event_at ? Date.parse(a.last_event_at) : 0;
    const bT = b.last_event_at ? Date.parse(b.last_event_at) : 0;
    return bT - aT;
  });
  return items;
});

const counters = computed(() => dash.counters);

// Hosts sorted by verdict severity: red first, then amber, then green.
// Uses the same thresholds as HostCapacityChip without duplicating colour logic.
function hostSeverity(h: HostSummary): number {
  const gpuUtil = h.gpu_util_pct != null ? Math.round(h.gpu_util_pct) : null;
  const vramPct = h.gpu_mem_mb != null && h.gpu_mem_total_mb ? Math.round((h.gpu_mem_mb / h.gpu_mem_total_mb) * 100) : null;
  const ramPct = h.ram_mb != null && h.ram_total_mb ? Math.round((h.ram_mb / h.ram_total_mb) * 100) : null;
  const metrics = [gpuUtil, vramPct, ramPct].filter((v): v is number => v != null);
  if (metrics.some((v) => v >= 85)) return 2; // red
  if (metrics.some((v) => v >= 60)) return 1; // amber
  return 0; // green
}

const sortedHostsByCapacity = computed<HostSummary[]>(() => {
  return [...dash.hosts].sort((a, b) => hostSeverity(b) - hostSeverity(a));
});

function refresh() {
  void dash.fetch();
  void fetchRunningBatches();
}

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

onMounted(() => {
  void stars.ensureLoaded();
  // Pick up current scope saved in the store.
  projectFilter.value = dash.scope;
  dash.start();
  void fetchRunningBatches();
});
onUnmounted(() => dash.stop());
</script>

<template>
  <div class="page-container">
    <!-- Header row -->
    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap">
      <div style="font-size: 18px; font-weight: 600">{{ t('page_dashboard.title') }}</div>
      <span class="muted" style="font-size: 12px">
        <template v-if="dash.lastFetchedAt">{{ t('page_dashboard.updated', { time: fmtRelative(new Date(dash.lastFetchedAt).toISOString()) }) }}</template>
      </span>

      <a-tabs :active-key="projectFilter" style="margin-left: 16px" @change="onScopeChange">
        <a-tab-pane key="mine" :tab="t('page_dashboard.scope_mine')" />
        <a-tab-pane key="shared" :tab="t('page_dashboard.scope_shared')" />
        <a-tab-pane v-if="auth.isAdmin" key="all" :tab="t('page_dashboard.scope_all')" />
      </a-tabs>

      <span style="flex: 1" />

      <a-tooltip :title="t('page_dashboard.auto_refresh')">
        <a-switch
          :checked="dash.autoRefresh"
          :checked-children="t('page_dashboard.auto_on')"
          :un-checked-children="t('page_dashboard.auto_off')"
          @change="(v: boolean | string | number) => dash.setAutoRefresh(Boolean(v))"
        />
      </a-tooltip>
      <a-input-number
        :value="dash.refreshSec"
        :min="5"
        :max="600"
        :step="5"
        addon-after="s"
        style="width: 90px"
        @change="(v: number | string | undefined) => dash.setRefreshSec(Number(v) || 10)"
      />
      <a-button type="primary" :loading="dash.loading" @click="refresh">
        <template #icon><ReloadOutlined /></template>
        {{ t('common.refresh') }}
      </a-button>
    </div>

    <!-- Top metric tiles (requirements §16.2) -->
    <a-row :gutter="[12, 12]" style="margin-bottom: 16px">
      <a-col :xs="12" :sm="8" :md="4">
        <MetricTile
          :label="t('page_dashboard.tile_running_batches')"
          :value="counters.running_batches ?? 0"
          tone="info"
          clickable
          @click="router.push('/batches?scope=mine&status=running')"
        />
      </a-col>
      <a-col :xs="12" :sm="8" :md="4">
        <MetricTile
          :label="t('page_dashboard.tile_jobs_running')"
          :value="counters.jobs_running ?? 0"
          tone="info"
          clickable
          @click="router.push('/jobs?status=running')"
        />
      </a-col>
      <a-col :xs="12" :sm="8" :md="4">
        <MetricTile
          :label="t('page_dashboard.tile_done_24h')"
          :value="counters.jobs_done_24h ?? 0"
          tone="success"
          clickable
          @click="router.push('/jobs?status=done&since=24h')"
        />
      </a-col>
      <a-col :xs="12" :sm="8" :md="4">
        <MetricTile
          :label="t('page_dashboard.tile_failed_24h')"
          :value="counters.jobs_failed_24h ?? 0"
          :tone="(counters.jobs_failed_24h ?? 0) > 0 ? 'danger' : 'default'"
          clickable
          @click="router.push('/jobs?status=failed&since=24h')"
        />
      </a-col>
      <a-col :xs="12" :sm="8" :md="4">
        <MetricTile
          :label="t('page_dashboard.tile_active_hosts')"
          :value="counters.active_hosts ?? 0"
          tone="default"
          clickable
          @click="router.push('/hosts')"
        />
      </a-col>
      <a-col :xs="12" :sm="8" :md="4">
        <MetricTile
          :label="t('page_dashboard.tile_avg_gpu_util')"
          :value="
            counters.avg_gpu_util != null && Number.isFinite(counters.avg_gpu_util as number)
              ? Math.round((counters.avg_gpu_util as number) * 100)
              : null
          "
          suffix="%"
          :tone="(counters.avg_gpu_util ?? 0) > 0.6 ? 'success' : 'default'"
        />
      </a-col>
    </a-row>

    <!-- Host capacity rail -->
    <div style="margin-bottom: 16px">
      <a-card size="small" :body-style="{ padding: '10px 12px' }">
        <template #title>
          <span style="font-size: 13px">{{ $t('page_dashboard.host_capacity_title') }}</span>
        </template>
        <template #extra>
          <a-button size="small" type="link" @click="router.push('/hosts')">
            {{ $t('common.view_all') }}
          </a-button>
        </template>
        <div
          v-if="!sortedHostsByCapacity.length"
          class="muted empty-wrap"
          style="padding: 12px 0"
        >
          {{ $t('page_dashboard.host_capacity_empty') }}
        </div>
        <div
          v-else
          style="display: flex; flex-wrap: wrap; gap: 10px"
        >
          <HostCapacityChip
            v-for="h in sortedHostsByCapacity"
            :key="h.host"
            :host="h"
          />
        </div>
      </a-card>
    </div>

    <!-- Running batches compact section — kept above GPU hours because it's
         the primary "what's happening right now" surface. -->
    <div
      v-if="runningBatches.length > 0"
      style="margin-bottom: 16px"
    >
      <a-card
        size="small"
        :body-style="{ padding: '10px 12px' }"
      >
        <template #title>
          <span style="font-size: 13px">{{ $t('page_dashboard.running_batches_title') }}</span>
        </template>
        <template #extra>
          <a-button size="small" type="link" @click="router.push('/batches?scope=mine&status=running')">
            {{ $t('common.view_all') }}
          </a-button>
        </template>
        <a-row :gutter="[12, 12]">
          <a-col
            v-for="item in runningBatches"
            :key="item.batch.id"
            :xs="24"
            :sm="12"
            :xl="8"
            @mouseenter="onBatchHover(item.batch.id)"
            @mouseleave="onBatchHoverEnd(item.batch.id)"
          >
            <BatchCompactCard
              :compact-data="compactItemToBatchCompactData(item)"
              :refresh-key="dash.lastFetchedAt"
            />
          </a-col>
        </a-row>
      </a-card>
    </div>

    <!-- Main 3-column body (left 16, right 8 at md+) -->
    <a-row :gutter="[16, 16]">
      <a-col :xs="24" :md="16">
        <!-- Projects grid -->
        <a-card
          size="small"
          :title="t('page_dashboard.projects_card_title')"
          :bodyStyle="{ padding: '12px' }"
          style="margin-bottom: 16px"
        >
          <template #extra>
            <a-button size="small" type="link" @click="router.push('/projects')">{{ t('common.view_all') }}</a-button>
          </template>

          <div v-if="!orderedProjects.length && !dash.loading" class="muted empty-wrap" style="padding: 24px">
            {{ t('page_dashboard.projects_empty', { source_project: 'source.project' }) }}
          </div>

          <!-- Skeleton on first load so the grid doesn't pop in blank. -->
          <a-row v-if="dash.loading && !orderedProjects.length" :gutter="[12, 12]">
            <a-col
              v-for="i in 6"
              :key="`skel-${i}`"
              :xs="24"
              :sm="12"
              :xl="8"
            >
              <a-card size="small" :body-style="{ padding: '12px 14px' }">
                <a-skeleton active :title="{ width: '50%' }" :paragraph="{ rows: 2 }" />
              </a-card>
            </a-col>
          </a-row>
          <a-row v-else :gutter="[12, 12]">
            <a-col
              v-for="p in orderedProjects"
              :key="p.project"
              :xs="24"
              :sm="12"
              :xl="8"
              @mouseenter="onProjectHover(p.project)"
              @mouseleave="onProjectHoverEnd(p.project)"
            >
              <ProjectCard :project="p" />
            </a-col>
          </a-row>
        </a-card>

        <!-- Per-user GPU hours tile — placed below projects for secondary context -->
        <div style="margin-bottom: 16px">
          <GpuHoursTile />
        </div>

        <!-- Activity feed -->
        <a-card size="small" :title="t('page_dashboard.activity_card_title')" :bodyStyle="{ padding: '12px' }">
          <ActivityFeed :items="dash.activity" :max-items="20" />
        </a-card>
      </a-col>

      <a-col :xs="24" :md="8">
        <!-- Hosts rail -->
        <a-card size="small" :title="t('page_dashboard.hosts_card_title')" :bodyStyle="{ padding: '12px' }" style="margin-bottom: 16px">
          <template #extra>
            <a-button size="small" type="link" @click="router.push('/hosts')">{{ t('common.view_all') }}</a-button>
          </template>
          <div v-if="!dash.hosts.length" class="muted empty-wrap" style="padding: 16px">
            {{ t('page_dashboard.hosts_empty') }}
          </div>
          <div v-else style="display: flex; flex-direction: column; gap: 10px">
            <HostCard v-for="h in dash.hosts" :key="h.host" :host="h" />
          </div>
        </a-card>

        <!-- Notifications -->
        <a-card
          size="small"
          :bodyStyle="{ padding: '12px' }"
          style="margin-bottom: 16px"
        >
          <template #title>
            <NotificationOutlined style="margin-right: 6px" />
            {{ t('page_dashboard.notifications_card_title') }}
          </template>
          <div v-if="!dash.notifications.length" class="muted empty-wrap" style="padding: 16px">
            {{ t('page_dashboard.notifications_empty') }}
          </div>
          <div v-else style="display: flex; flex-direction: column; gap: 8px">
            <a-alert
              v-for="(n, i) in dash.notifications"
              :key="n.id ?? `${n.timestamp}-${i}`"
              :type="n.level === 'error' ? 'error' : n.level === 'warn' ? 'warning' : 'info'"
              :message="n.title"
              :description="n.body || fmtRelative(n.timestamp)"
              show-icon
              closable
            />
          </div>
        </a-card>

        <!-- Quick actions -->
        <a-card size="small" :title="t('page_dashboard.quick_actions_title')" :bodyStyle="{ padding: '12px' }">
          <a-space direction="vertical" style="width: 100%">
            <a-button block @click="router.push('/settings/tokens')">
              <template #icon><KeyOutlined /></template>
              {{ t('page_dashboard.action_generate_token') }}
            </a-button>
            <a-button block @click="router.push('/settings/shares')">
              <template #icon><ShareAltOutlined /></template>
              {{ t('page_dashboard.action_manage_shares') }}
            </a-button>
            <a-button block @click="router.push('/compare')">
              <template #icon><ThunderboltOutlined /></template>
              {{ t('page_dashboard.action_open_compare') }}
            </a-button>
          </a-space>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>
