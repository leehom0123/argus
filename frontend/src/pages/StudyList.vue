<script setup lang="ts">
/**
 * StudyList.vue (v0.2 hyperopt-ui) — Optuna multirun overview.
 *
 * Lists every study the caller can see, one row per ``optuna.study_name``
 * with summary aggregates (n_trials / n_done / best_value / sampler /
 * last_run). Clicking a row drills into ``/studies/:name`` for the
 * trial-level table.
 *
 * Empty state shows a hint pointing at the Hydra ``/hparams_search``
 * pattern so users who haven't run a sweep yet know where to start.
 */
import { onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRouter } from 'vue-router';
import { ReloadOutlined, ExperimentOutlined } from '@ant-design/icons-vue';
import EmptyState from '../components/EmptyState.vue';
import { listStudies, type StudySummary } from '../api/studies';
import { fmtRelative } from '../utils/format';

const { t } = useI18n();
const router = useRouter();

const studies = ref<StudySummary[]>([]);
const loading = ref<boolean>(false);

async function refresh() {
  loading.value = true;
  try {
    const res = await listStudies();
    studies.value = res.studies;
  } finally {
    loading.value = false;
  }
}

function openStudy(name: string) {
  router.push(`/studies/${encodeURIComponent(name)}`);
}

function fmtBest(v: number | null): string {
  if (v === null || v === undefined) return '—';
  // Optuna best values are typically losses in [1e-4, 10]; show 4 sig figs
  // for the headline column.
  return Math.abs(v) >= 1000 || (Math.abs(v) < 0.001 && v !== 0)
    ? v.toExponential(3)
    : v.toFixed(4);
}

onMounted(refresh);
</script>

<template>
  <div style="padding: 16px 20px">
    <div
      style="
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 16px;
      "
    >
      <h2 style="margin: 0">
        <ExperimentOutlined style="margin-right: 8px" />
        {{ t('page_studies.title') }}
      </h2>
      <a-button :loading="loading" @click="refresh">
        <template #icon><ReloadOutlined /></template>
        {{ t('common.refresh') }}
      </a-button>
    </div>

    <a-table
      v-if="studies.length > 0 || loading"
      :data-source="studies"
      :loading="loading"
      :pagination="false"
      row-key="study_name"
      :custom-row="
        (record: StudySummary) => ({
          onClick: () => openStudy(record.study_name),
          style: { cursor: 'pointer' },
        })
      "
    >
      <a-table-column
        :title="t('page_studies.col_name')"
        data-index="study_name"
        key="study_name"
      >
        <template #default="{ record }">
          <strong>{{ record.study_name }}</strong>
        </template>
      </a-table-column>

      <a-table-column
        :title="t('page_studies.col_n_trials')"
        data-index="n_trials"
        key="n_trials"
        align="right"
      >
        <template #default="{ record }">
          {{ record.n_trials }}
          <a-tag
            v-if="record.n_failed > 0"
            color="red"
            style="margin-left: 6px; font-size: 10px"
          >
            {{ record.n_failed }} {{ t('page_studies.failed') }}
          </a-tag>
        </template>
      </a-table-column>

      <a-table-column
        :title="t('page_studies.col_best')"
        data-index="best_value"
        key="best_value"
        align="right"
      >
        <template #default="{ record }">
          <span style="font-family: var(--font-mono, ui-monospace, monospace)">
            {{ fmtBest(record.best_value) }}
          </span>
          <a-tag
            v-if="record.best_metric"
            color="blue"
            style="margin-left: 6px; font-size: 10px"
          >
            {{ record.best_metric }}
          </a-tag>
        </template>
      </a-table-column>

      <a-table-column
        :title="t('page_studies.col_direction')"
        data-index="direction"
        key="direction"
      >
        <template #default="{ record }">
          <a-tag v-if="record.direction" :color="record.direction === 'minimize' ? 'green' : 'orange'">
            {{ record.direction }}
          </a-tag>
          <span v-else>—</span>
        </template>
      </a-table-column>

      <a-table-column
        :title="t('page_studies.col_sampler')"
        data-index="sampler"
        key="sampler"
      >
        <template #default="{ record }">
          <span>{{ record.sampler ?? '—' }}</span>
        </template>
      </a-table-column>

      <a-table-column
        :title="t('page_studies.col_last_run')"
        data-index="last_run"
        key="last_run"
      >
        <template #default="{ record }">
          <span>{{ record.last_run ? fmtRelative(record.last_run) : '—' }}</span>
        </template>
      </a-table-column>
    </a-table>

    <EmptyState
      v-else
      variant="empty_studies"
      :title="t('page_studies.empty_title')"
      :hint="t('page_studies.empty_hint')"
    />
  </div>
</template>
