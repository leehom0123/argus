<script setup lang="ts">
/**
 * StudyDetail.vue (v0.2 hyperopt-ui) — single-Optuna-study trial table.
 *
 * Renders every trial in the study with hyperparameter columns + the
 * headline value column (sortable asc/desc; default = best on top).
 * Each row deep-links to ``/batches/:batch/jobs/:job`` so users can
 * follow a promising trial all the way down to its loss curves.
 */
import { computed, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import { ReloadOutlined } from '@ant-design/icons-vue';
import EmptyState from '../components/EmptyState.vue';
import {
  getStudy,
  type StudyDetailOut,
  type StudySortKey,
  type StudySortOrder,
  type TrialRow,
} from '../api/studies';
import { fmtDuration, fmtTime } from '../utils/format';

const { t } = useI18n();
const route = useRoute();
const router = useRouter();

const props = defineProps<{ name: string }>();

const data = ref<StudyDetailOut | null>(null);
const loading = ref<boolean>(false);
const sortKey = ref<StudySortKey>('value');
const sortOrder = ref<StudySortOrder>('asc');

async function refresh() {
  loading.value = true;
  try {
    data.value = await getStudy(props.name, {
      sort: sortKey.value,
      order: sortOrder.value,
    });
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.name, sortKey.value, sortOrder.value],
  () => {
    void refresh();
  },
);

onMounted(refresh);

const paramKeys = computed<string[]>(() => data.value?.param_keys ?? []);

function fmtParam(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') {
    return Math.abs(value) < 0.001 && value !== 0
      ? value.toExponential(3)
      : String(value);
  }
  return String(value);
}

function fmtValue(v: number | null): string {
  if (v === null || v === undefined) return '—';
  return Math.abs(v) >= 1000 || (Math.abs(v) < 0.001 && v !== 0)
    ? v.toExponential(3)
    : v.toFixed(4);
}

function statusColor(s: string | null): string {
  switch ((s ?? '').toLowerCase()) {
    case 'done':
      return 'green';
    case 'failed':
      return 'red';
    case 'running':
      return 'blue';
    default:
      return 'default';
  }
}

function openTrial(row: TrialRow) {
  router.push(
    `/batches/${encodeURIComponent(row.batch_id)}/jobs/${encodeURIComponent(row.job_id)}`,
  );
}

function back() {
  router.push('/studies');
}
</script>

<template>
  <div style="padding: 16px 20px">
    <a-button size="small" style="margin-bottom: 12px" @click="back">
      ← {{ t('page_studies.back_to_list') }}
    </a-button>

    <div
      style="
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 12px;
      "
    >
      <div>
        <h2 style="margin: 0">{{ name }}</h2>
        <div style="opacity: 0.65; font-size: 13px; margin-top: 4px">
          <span v-if="data?.direction">{{ data.direction }}</span>
          <span v-if="data?.sampler"> · {{ data.sampler }}</span>
          <span v-if="data"> · {{ data.n_trials }} {{ t('page_studies.trials') }}</span>
          <span v-if="data && data.n_failed > 0">
            ·
            <a-tag color="red" style="font-size: 10px">
              {{ data.n_failed }} {{ t('page_studies.failed') }}
            </a-tag>
          </span>
        </div>
      </div>
      <a-button :loading="loading" @click="refresh">
        <template #icon><ReloadOutlined /></template>
        {{ t('common.refresh') }}
      </a-button>
    </div>

    <div
      v-if="data && data.best_value !== null"
      style="margin-bottom: 12px; padding: 10px 14px;
             background: rgba(64, 150, 255, 0.08); border-radius: 6px"
    >
      <strong>{{ t('page_studies.best') }}:</strong>
      <span style="font-family: var(--font-mono, ui-monospace, monospace); margin-left: 8px">
        {{ fmtValue(data.best_value) }}
      </span>
      <a-tag v-if="data.best_metric" color="blue" style="margin-left: 6px">
        {{ data.best_metric }}
      </a-tag>
    </div>

    <div style="display: flex; gap: 8px; margin-bottom: 8px">
      <a-radio-group v-model:value="sortKey" size="small">
        <a-radio-button value="value">{{ t('page_studies.sort_value') }}</a-radio-button>
        <a-radio-button value="trial_id">{{ t('page_studies.sort_trial_id') }}</a-radio-button>
        <a-radio-button value="start_time">{{ t('page_studies.sort_start') }}</a-radio-button>
      </a-radio-group>
      <a-radio-group v-model:value="sortOrder" size="small">
        <a-radio-button value="asc">{{ t('page_studies.order_asc') }}</a-radio-button>
        <a-radio-button value="desc">{{ t('page_studies.order_desc') }}</a-radio-button>
      </a-radio-group>
    </div>

    <a-table
      v-if="data && data.trials.length > 0"
      :data-source="data.trials"
      :loading="loading"
      :pagination="{ pageSize: 50, showSizeChanger: false }"
      row-key="trial_id"
      :custom-row="
        (record: TrialRow) => ({
          onClick: () => openTrial(record),
          style: { cursor: 'pointer' },
        })
      "
      size="small"
    >
      <a-table-column
        :title="t('page_studies.col_trial_id')"
        data-index="trial_id"
        key="trial_id"
        align="right"
      >
        <template #default="{ record }">
          <strong>#{{ record.trial_id }}</strong>
        </template>
      </a-table-column>

      <a-table-column
        :title="t('page_studies.col_value')"
        data-index="value"
        key="value"
        align="right"
      >
        <template #default="{ record }">
          <span style="font-family: var(--font-mono, ui-monospace, monospace)">
            {{ fmtValue(record.value) }}
          </span>
        </template>
      </a-table-column>

      <a-table-column
        v-for="key in paramKeys"
        :key="`param-${key}`"
        :title="key"
        align="right"
      >
        <template #default="{ record }">
          <span style="font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px">
            {{ fmtParam(record.params?.[key]) }}
          </span>
        </template>
      </a-table-column>

      <a-table-column
        :title="t('page_studies.col_status')"
        data-index="status"
        key="status"
      >
        <template #default="{ record }">
          <a-tag :color="statusColor(record.status)">{{ record.status ?? '—' }}</a-tag>
        </template>
      </a-table-column>

      <a-table-column
        :title="t('page_studies.col_elapsed')"
        data-index="elapsed_s"
        key="elapsed_s"
        align="right"
      >
        <template #default="{ record }">
          {{ record.elapsed_s !== null ? fmtDuration(record.elapsed_s) : '—' }}
        </template>
      </a-table-column>

      <a-table-column
        :title="t('page_studies.col_start')"
        data-index="start_time"
        key="start_time"
      >
        <template #default="{ record }">
          {{ record.start_time ? fmtTime(record.start_time) : '—' }}
        </template>
      </a-table-column>
    </a-table>

    <EmptyState
      v-else-if="!loading"
      variant="empty_study_trials"
      :title="t('page_studies.empty_detail_title')"
      :hint="t('page_studies.empty_detail_hint')"
    />
  </div>
</template>
