<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import { ReloadOutlined, ClearOutlined, ArrowLeftOutlined } from '@ant-design/icons-vue';
import { useCompareStore, COMPARE_LIMIT } from '../store/compare';
import { usePinsStore, PIN_LIMIT } from '../store/pins';
import { exportCompareCsv } from '../api/exports';
import { notification } from 'ant-design-vue';
import ExportCsvButton from '../components/ExportCsvButton.vue';
import { useChart } from '../composables/useChart';
import type { CompareBatch, EpochPoint } from '../types';

const { t } = useI18n();
const store = useCompareStore();
const pins = usePinsStore();
const route = useRoute();
const router = useRouter();

// ---- selection bootstrap ----

function parseQueryBatches(): string[] {
  const raw = route.query.batches;
  if (!raw) return [];
  const str = Array.isArray(raw) ? raw.join(',') : String(raw);
  return str
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

async function bootstrap() {
  await pins.ensureLoaded();
  const fromQuery = parseQueryBatches();
  if (fromQuery.length >= 2) {
    if (fromQuery.length > COMPARE_LIMIT) {
      notification.warning({
        message: t('page_compare.too_many', { max: COMPARE_LIMIT }),
        duration: 4,
      });
    }
    store.setSelection(fromQuery);
  } else if (pins.ids.length >= 2) {
    store.setSelection(pins.ids);
  }
  if (store.canFetch) {
    await store.fetch();
  }
}

onMounted(() => {
  void bootstrap();
});

// Keep URL in sync with selection so it's shareable.
watch(
  () => store.selection,
  (ids) => {
    const qs = ids.length ? { batches: ids.join(',') } : {};
    router.replace({ path: '/compare', query: qs });
  },
);

// ---- derived ----

const batches = computed<CompareBatch[]>(() => store.data?.batches ?? []);

const metricKeys = computed<string[]>(() => {
  if (store.data?.metric_keys?.length) return store.data.metric_keys;
  // Fall back to union of keys across batches.
  const set = new Set<string>();
  for (const b of batches.value) {
    const m = b.metrics ?? {};
    for (const k of Object.keys(m)) {
      if (typeof m[k] === 'number') set.add(k);
    }
  }
  return Array.from(set).sort();
});

const metricTableColumns = computed(() => [
  { title: t('common.metric'), dataIndex: 'metric', key: 'metric', width: 120, fixed: 'left' as const },
  ...batches.value.map((b) => ({
    title: b.batch_id,
    dataIndex: b.batch_id,
    key: b.batch_id,
    width: 200,
  })),
]);

const metricTableRows = computed(() => {
  return metricKeys.value.map((k) => {
    const row: Record<string, string | number> = { metric: k };
    for (const b of batches.value) {
      const v = b.metrics?.[k];
      row[b.batch_id] = typeof v === 'number' ? Number(v.toFixed(4)) : '—';
    }
    return row;
  });
});

// ---- loss chart ----

const lossChartEl = ref<HTMLElement | null>(null);
const palette = ['#4096ff', '#52c41a', '#faad14', '#ff4d4f'];

const lossOption = computed(() => {
  const bs = batches.value;
  if (!bs.length) return null;
  const xsSet = new Set<number>();
  bs.forEach((b) => (b.loss_curve ?? []).forEach((p) => xsSet.add(p.epoch)));
  const xs = Array.from(xsSet).sort((a, b) => a - b);
  const series = bs.map((b, idx) => {
    const byEpoch = new Map<number, EpochPoint>();
    (b.loss_curve ?? []).forEach((p) => byEpoch.set(p.epoch, p));
    const data = xs.map((x) => byEpoch.get(x)?.val_loss ?? null);
    return {
      name: b.batch_id,
      type: 'line',
      data,
      smooth: true,
      showSymbol: false,
      connectNulls: true,
      lineStyle: { color: palette[idx % palette.length] },
      itemStyle: { color: palette[idx % palette.length] },
    };
  });

  return {
    backgroundColor: 'transparent',
    grid: { left: 55, right: 20, top: 40, bottom: 40 },
    tooltip: { trigger: 'axis' },
    legend: { top: 4, data: bs.map((b) => b.batch_id) },
    xAxis: { type: 'category', data: xs, name: 'epoch', nameLocation: 'middle', nameGap: 25 },
    yAxis: { type: 'value', name: 'val_loss', scale: true },
    series,
  };
});

useChart(lossChartEl, lossOption);

// ---- actions ----

function unpinAll() {
  void pins.clearAll();
  store.clear();
}

function refresh() {
  void store.fetch();
}

function removeBatch(id: string) {
  store.remove(id);
  if (store.canFetch) void store.fetch();
  else store.data = null;
}

const canExport = computed(() => store.canFetch);
</script>

<template>
  <div class="page-container">
    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap">
      <a-button size="small" @click="router.back()">
        <template #icon><ArrowLeftOutlined /></template>
        {{ $t('common.back') }}
      </a-button>
      <div style="font-size: 18px; font-weight: 600">{{ $t('page_compare.title') }}</div>
      <span class="muted" style="font-size: 12px">
        {{ $t('page_compare.selected', { count: store.count, limit: COMPARE_LIMIT }) }}
      </span>
      <span style="flex: 1" />
      <a-button size="small" :loading="store.loading" @click="refresh">
        <template #icon><ReloadOutlined /></template>
        {{ $t('common.refresh') }}
      </a-button>
      <ExportCsvButton
        :label="$t('page_compare.export_csv')"
        :disabled="!canExport"
        :handler="() => exportCompareCsv(store.selection)"
      />
      <a-popconfirm
        :title="$t('page_compare.unpin_confirm_title')"
        :description="$t('page_compare.unpin_confirm_desc')"
        :ok-text="$t('page_compare.unpin_ok')"
        ok-type="danger"
        :cancel-text="$t('common.cancel')"
        @confirm="unpinAll"
      >
        <a-button size="small" danger :disabled="!pins.count">
          <template #icon><ClearOutlined /></template>
          {{ $t('page_compare.unpin_all') }}
        </a-button>
      </a-popconfirm>
    </div>

    <a-alert
      v-if="store.count < 2"
      type="info"
      show-icon
      :message="$t('page_compare.info_message')"
      :description="$t('page_compare.info_desc', { max: COMPARE_LIMIT })"
      style="margin-bottom: 12px"
    />

    <!-- Batch chips -->
    <div v-if="store.count" style="margin-bottom: 12px; display: flex; flex-wrap: wrap; gap: 6px">
      <a-tag
        v-for="(id, idx) in store.selection"
        :key="id"
        :color="['blue', 'green', 'orange', 'red'][idx % 4]"
        closable
        @close="removeBatch(id)"
      >
        {{ id }}
      </a-tag>
    </div>

    <!-- Loss comparison -->
    <a-card v-if="canExport && batches.length" size="small" :title="$t('page_compare.card_loss_title')" style="margin-bottom: 16px">
      <div ref="lossChartEl" style="width: 100%; height: 360px" />
    </a-card>

    <!-- Metrics table -->
    <a-card v-if="batches.length" size="small" :title="$t('page_compare.card_metrics_title')" style="margin-bottom: 16px">
      <a-table
        :columns="metricTableColumns"
        :data-source="metricTableRows"
        row-key="metric"
        size="small"
        :pagination="false"
        :scroll="{ x: 120 + batches.length * 200 }"
      />
    </a-card>

    <!-- Per-batch summary cards -->
    <a-row v-if="batches.length" :gutter="[12, 12]">
      <a-col
        v-for="b in batches"
        :key="b.batch_id"
        :xs="24"
        :sm="12"
        :md="Math.max(6, Math.floor(24 / batches.length))"
      >
        <a-card size="small" :bodyStyle="{ padding: '10px 12px' }">
          <div
            style="font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600"
          >
            {{ b.batch_id }}
          </div>
          <div class="muted" style="font-size: 11px; margin-top: 2px">
            {{ b.model || '—' }} × {{ b.dataset || '—' }} · {{ b.status || '—' }}
          </div>
          <div style="margin-top: 8px; font-size: 12px; line-height: 1.6">
            <template v-if="b.metrics">
              <div v-for="(v, k) in b.metrics" :key="k">
                <span class="muted">{{ k }}:</span>
                <span style="font-variant-numeric: tabular-nums">
                  {{ typeof v === 'number' ? v.toFixed(4) : '—' }}
                </span>
              </div>
            </template>
          </div>
          <a-button
            size="small"
            style="margin-top: 8px"
            @click="router.push(`/batches/${encodeURIComponent(b.batch_id)}`)"
          >
            {{ $t('page_compare.open_batch') }}
          </a-button>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>
