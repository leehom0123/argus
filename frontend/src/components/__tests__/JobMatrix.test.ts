/**
 * JobMatrix.test.ts
 *
 * Pins behaviour for the redesigned Batch → Matrix tab. After #126 the
 * matrix moved from "every cell tinted" to "white default + selective
 * best/worst highlight + corner status dot", so this file covers:
 *
 *   1. Cells render single or multi-metric (1-3 slash-separated) values.
 *   2. Best cell — single global best across the matrix on the primary
 *      metric — gets a thicker green border + trophy icon.
 *   3. Worst cell gets a thicker red border + warning icon.
 *   4. ``status === 'failed' / 'running'`` and ``is_idle_flagged`` cells
 *      are skipped from best/worst designation.
 *   5. Direction inference (lower-better vs higher-better) by metric name.
 *   6. Edge cases: all-equal metrics, single-cell batch, all-failed batch.
 *   7. Status dot — colour, presence, absence on clean ``done`` cells.
 *   8. Hover popover lists every metric of the job.
 *   9. localStorage persistence of the user's metric selection.
 *
 * Runs under jsdom because we mount Vue components and exercise
 * localStorage. We stub Ant Design components heavily to keep render
 * synchronous and free of teleport / popover side effects.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';
import JobMatrix from '../JobMatrix.vue';
import type { Job } from '../../types';

// ---------------------------------------------------------------------------
// Minimal i18n instance — only carries the keys JobMatrix actually reads.
// ---------------------------------------------------------------------------
function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    messages: {
      'en-US': {
        component_job_matrix: {
          empty: 'No jobs yet for this batch.',
          no_job: 'No job',
          status: 'status: {status}',
          elapsed: 'elapsed: {elapsed}s',
        },
        component_job_matrix_popover: {
          title: 'Run details',
          label_status: 'Status',
          label_elapsed: 'Elapsed',
          label_model: 'Model',
          label_dataset: 'Dataset',
          label_avg_batch_time: 'Avg batch time',
          label_gpu_peak: 'GPU peak',
          label_n_params: 'Parameters',
          metrics_title: 'Metrics',
          no_metrics: 'No metrics reported yet.',
        },
        component_job_badge: {
          idle: 'Idle',
          idle_tooltip_generic: 'GPU utilization low',
          idle_tooltip_minutes: 'GPU util < 5% for {minutes} minutes',
        },
        component_empty_state: {
          empty_jobs: "This batch has no jobs yet — they'll appear as the sweep starts.",
        },
        matrix: {
          title: 'Job matrix',
          metrics_label: 'Metrics',
          metric_max_warning: 'At most 3 metrics',
          best_chip: 'Best so far',
          direction_question: 'Direction unknown',
          job_count: '{n} jobs',
          best: 'Best in batch',
          worst: 'Worst in batch',
          failed: 'Failed',
          stalled: 'Stalled',
        },
      },
    },
  });
}

// ---------------------------------------------------------------------------
// Stubs — Ant components are reduced to lightweight DOM so our class
// assertions hit the real cell elements rather than teleported portals.
// ---------------------------------------------------------------------------
const globalStubs = {
  APopover: {
    inheritAttrs: false,
    template: '<div class="popover-stub"><slot /></div>',
  },
  ASelect: {
    props: ['value', 'options'],
    emits: ['update:value'],
    template: `
      <select
        class="metric-select-stub"
        multiple
        @change="$emit('update:value', Array.from($event.target.selectedOptions).map(o => o.value))"
      >
        <option
          v-for="opt in (options || [])"
          :key="opt.value"
          :value="opt.value"
          :selected="(value || []).includes(opt.value)"
        >{{ opt.label }}</option>
      </select>
    `,
  },
  TrophyOutlined: { template: '<span class="trophy-stub" />' },
  WarningFilled: { template: '<span class="warning-stub" />' },
};

function mountMatrix(jobs: Job[], experimentName: string | null = 'bench-multi') {
  return mount(JobMatrix, {
    props: { jobs, experimentName },
    global: {
      plugins: [makeI18n()],
      stubs: globalStubs,
    },
  });
}

// ---------------------------------------------------------------------------
// Job factories — build small synthetic batches with controlled metrics.
// ---------------------------------------------------------------------------
function mkJob(
  model: string,
  dataset: string,
  metrics: Record<string, number>,
  extra: Partial<Job> = {},
): Job {
  return {
    id: `${model}-${dataset}`,
    batch_id: 'b1',
    model,
    dataset,
    status: 'done',
    metrics,
    ...extra,
  };
}

const STORAGE_KEY = 'argus.batch-matrix.metrics';

describe('JobMatrix component', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  // -------------------------------------------------------------------------
  // 1. Single metric — preserves the pre-multi-metric default rendering.
  // -------------------------------------------------------------------------
  it('renders cells with a single metric on first load', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.500 }),
      mkJob('A', 'd2', { MSE: 0.100 }),
      mkJob('B', 'd1', { MSE: 0.300 }),
      mkJob('B', 'd2', { MSE: 0.200 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    const cells = w.findAll('.matrix-cell');
    expect(cells.length).toBe(4);
    // Each cell shows exactly one metric piece (no slash separator).
    const primaries = w.findAll('.metric-primary');
    expect(primaries.length).toBe(4);
    expect(w.findAll('.metric-secondary').length).toBe(0);
    // The MSE values should be present, formatted to 3 decimals.
    const text = primaries.map((p) => p.text()).join(' ');
    expect(text).toContain('0.500');
    expect(text).toContain('0.100');
  });

  // -------------------------------------------------------------------------
  // 2. Three metrics slash-separated when 3 selected via the select stub.
  // -------------------------------------------------------------------------
  it('renders cells with 3 slash-separated metrics when 3 are selected', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.5, MAE: 0.4, R2: 0.9 }),
      mkJob('B', 'd1', { MSE: 0.3, MAE: 0.2, R2: 0.95 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    // Force-set selection via the component's exposed selectedMetrics
    // by emitting through the stubbed select. The stub turns selectedOptions
    // into an emitted array. Easier: trigger update:value directly.
    const selectStub = w.findComponent({ name: 'ASelect' }) as ReturnType<typeof w.findComponent>;
    // Fall back to the underlying stub's element when component lookup misses.
    if (selectStub.exists()) {
      await selectStub.vm.$emit('update:value', ['MSE', 'MAE', 'R2']);
    } else {
      // Stubbed via globalStubs key; emit through DOM event.
      const native = w.find('.metric-select-stub');
      // Manually cause the @change handler to push all three values.
      Array.from(native.element.querySelectorAll('option')).forEach((o: Element) => {
        (o as HTMLOptionElement).selected = ['MSE', 'MAE', 'R2'].includes(
          (o as HTMLOptionElement).value,
        );
      });
      await native.trigger('change');
    }
    await w.vm.$nextTick();

    const firstCell = w.findAll('.matrix-cell')[0];
    // Three pieces: 1 primary + 2 secondary.
    expect(firstCell.findAll('.metric-primary').length).toBe(1);
    expect(firstCell.findAll('.metric-secondary').length).toBe(2);
    // Two slash separators in the cell text.
    expect((firstCell.text().match(/\//g) || []).length).toBe(2);
  });

  // -------------------------------------------------------------------------
  // 3. Best cell — thicker green border + trophy icon (#126).
  // -------------------------------------------------------------------------
  it('marks the single global best cell with cell-best class + trophy', async () => {
    // 2x2 matrix; B/d1 wins on MSE (lowest = 0.10).
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.50 }),
      mkJob('A', 'd2', { MSE: 0.40 }),
      mkJob('B', 'd1', { MSE: 0.10 }), // best
      mkJob('B', 'd2', { MSE: 0.30 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    const bestCells = w.findAll('.matrix-cell.cell-best');
    expect(bestCells.length).toBe(1);
    // Trophy icon appears inside the best cell only.
    expect(bestCells[0].find('[data-testid="cell-flag-best"]').exists()).toBe(true);
    // No other cell carries the trophy.
    expect(w.findAll('[data-testid="cell-flag-best"]').length).toBe(1);
  });

  // -------------------------------------------------------------------------
  // 4. Worst cell — thicker red border + warning icon.
  // -------------------------------------------------------------------------
  it('marks the single global worst cell with cell-worst class + warning', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.50 }), // worst
      mkJob('A', 'd2', { MSE: 0.20 }),
      mkJob('B', 'd1', { MSE: 0.10 }), // best
      mkJob('B', 'd2', { MSE: 0.30 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    const worstCells = w.findAll('.matrix-cell.cell-worst');
    expect(worstCells.length).toBe(1);
    expect(worstCells[0].find('[data-testid="cell-flag-worst"]').exists()).toBe(true);
    expect(w.findAll('[data-testid="cell-flag-worst"]').length).toBe(1);
  });

  // -------------------------------------------------------------------------
  // 4b. Stalled cell never receives the worst-flag, even if its metric is
  //     numerically the worst. Same applies to failed/running cells — best
  //     and worst are reserved for clean ``done`` runs.
  // -------------------------------------------------------------------------
  it('skips stalled / failed cells when picking best/worst', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.10 }), // done — should be best
      mkJob('A', 'd2', { MSE: 0.20 }), // done — mid
      // C/d1 has the *worst* number but is idle-flagged → must NOT be flagged worst.
      mkJob('B', 'd1', { MSE: 0.99 }, { is_idle_flagged: true }),
      // D/d1 has a high number but failed → must NOT be flagged worst.
      mkJob('B', 'd2', { MSE: 0.95 }, { status: 'failed' }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    // The eligible done cells are A/d1=0.10 and A/d2=0.20. Best is A/d1
    // (lowest); worst is A/d2 (highest among eligible).
    const cells = w.findAll('.matrix-cell');
    // Render order: A-d1, A-d2, B-d1, B-d2 (model rows × dataset cols, row-major).
    expect(cells[0].classes()).toContain('cell-best');
    expect(cells[1].classes()).toContain('cell-worst');
    expect(cells[2].classes()).not.toContain('cell-worst');
    expect(cells[2].classes()).not.toContain('cell-best');
    expect(cells[3].classes()).not.toContain('cell-worst');
    expect(cells[3].classes()).not.toContain('cell-best');
  });

  // -------------------------------------------------------------------------
  // 4c. All-equal-metric edge case — when every cell ties on the primary
  //     metric there's nothing to celebrate, so neither best nor worst
  //     gets flagged.
  // -------------------------------------------------------------------------
  it('skips highlighting when every eligible cell ties on the primary metric', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.25 }),
      mkJob('A', 'd2', { MSE: 0.25 }),
      mkJob('B', 'd1', { MSE: 0.25 }),
      mkJob('B', 'd2', { MSE: 0.25 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    expect(w.findAll('.matrix-cell.cell-best').length).toBe(0);
    expect(w.findAll('.matrix-cell.cell-worst').length).toBe(0);
    expect(w.findAll('[data-testid="cell-flag-best"]').length).toBe(0);
    expect(w.findAll('[data-testid="cell-flag-worst"]').length).toBe(0);
  });

  // -------------------------------------------------------------------------
  // 5. Direction inference — lower-better metric (MSE).
  // -------------------------------------------------------------------------
  it('treats MSE as lower-better (best = min value)', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.50 }),
      mkJob('B', 'd1', { MSE: 0.10 }), // min — should win cell-best
      mkJob('C', 'd1', { MSE: 0.30 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    const cells = w.findAll('.matrix-cell');
    expect(cells[1].classes()).toContain('cell-best');
    expect(cells[0].classes()).toContain('cell-worst');
  });

  // -------------------------------------------------------------------------
  // 6. Direction inference — higher-better metric (R2).
  // -------------------------------------------------------------------------
  it('treats R2 as higher-better (best = max value)', async () => {
    const jobs = [
      mkJob('A', 'd1', { R2: 0.50 }),
      mkJob('B', 'd1', { R2: 0.95 }), // max — should win cell-best
      mkJob('C', 'd1', { R2: 0.70 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    const cells = w.findAll('.matrix-cell');
    expect(cells[1].classes()).toContain('cell-best');
    expect(cells[0].classes()).toContain('cell-worst');
  });

  // -------------------------------------------------------------------------
  // 7. Unrecognised metric — falls back to lower-better behaviour.
  // -------------------------------------------------------------------------
  it('defaults unrecognised metrics (e.g. MyCustomLoss) to lower-better', async () => {
    const jobs = [
      mkJob('A', 'd1', { Wibble: 1.0 }),
      mkJob('B', 'd1', { Wibble: 0.5 }), // smaller — should be best when defaulted lower
      mkJob('C', 'd1', { Wibble: 1.5 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    const cells = w.findAll('.matrix-cell');
    expect(cells[1].classes()).toContain('cell-best');
    expect(cells[2].classes()).toContain('cell-worst');
  });

  // -------------------------------------------------------------------------
  // 7a. Single-cell batch — nothing to compare against, no highlight.
  // -------------------------------------------------------------------------
  it('skips highlighting on a single-cell batch', async () => {
    const jobs = [mkJob('A', 'd1', { MSE: 0.5 })];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    expect(w.findAll('.matrix-cell.cell-best').length).toBe(0);
    expect(w.findAll('.matrix-cell.cell-worst').length).toBe(0);
  });

  // -------------------------------------------------------------------------
  // 7b. Status dot — present for failed/running, absent for clean done.
  // -------------------------------------------------------------------------
  it('renders a status dot for non-done cells, omits it on clean done cells', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.1 }), // done, no idle — no dot
      mkJob('A', 'd2', { MSE: 0.5 }, { status: 'failed' }), // failed — dot
      mkJob('B', 'd1', { MSE: 0.3 }, { status: 'running' }), // running — pulsing dot
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    const cells = w.findAll('.matrix-cell');
    expect(cells[0].find('[data-testid="status-dot"]').exists()).toBe(false);
    expect(cells[1].find('[data-testid="status-dot"]').exists()).toBe(true);
    const runningDot = cells[2].find('[data-testid="status-dot"]');
    expect(runningDot.exists()).toBe(true);
    expect(runningDot.classes()).toContain('status-dot-pulse');
  });

  // -------------------------------------------------------------------------
  // 7c. All-failed batch edge case — no eligible cells, no highlight.
  // -------------------------------------------------------------------------
  it('skips highlighting when every cell failed', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.5 }, { status: 'failed' }),
      mkJob('A', 'd2', { MSE: 0.3 }, { status: 'failed' }),
      mkJob('B', 'd1', { MSE: 0.7 }, { status: 'failed' }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    expect(w.findAll('.matrix-cell.cell-best').length).toBe(0);
    expect(w.findAll('.matrix-cell.cell-worst').length).toBe(0);
  });

  // -------------------------------------------------------------------------
  // 8. Hover popover lists every metric of the job (verified via DOM presence
  //    of a metrics table built from job.metrics).
  // -------------------------------------------------------------------------
  it('renders a metrics table in the hover popover with every job metric', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.5, MAE: 0.4, R2: 0.9, PCC: 0.99 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    // Popover content is rendered eagerly under our APopover stub (slot
    // contents include both the trigger and the slot=content), so we can
    // assert the table is present and has 4 rows.
    const html = w.html();
    expect(html).toContain('MSE');
    expect(html).toContain('MAE');
    expect(html).toContain('R2');
    expect(html).toContain('PCC');
  });

  // -------------------------------------------------------------------------
  // 9. localStorage round-trip — selection survives remount.
  // -------------------------------------------------------------------------
  it('restores localStorage batchMatrixMetrics on remount', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.5, MAE: 0.4, R2: 0.9 }),
    ];
    // Pre-populate storage with a 2-metric pick.
    localStorage.setItem(STORAGE_KEY, JSON.stringify(['MAE', 'R2']));
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    // First metric piece should now be MAE (the new primary), and a
    // secondary piece for R2 — total of one primary + one secondary.
    expect(w.findAll('.metric-primary').length).toBe(1);
    expect(w.findAll('.metric-secondary').length).toBe(1);
  });

  // -------------------------------------------------------------------------
  // 10. localStorage write-through — picking metrics updates storage.
  // -------------------------------------------------------------------------
  it('persists the user selection to localStorage when changed', async () => {
    const jobs = [
      mkJob('A', 'd1', { MSE: 0.5, MAE: 0.4 }),
    ];
    const w = mountMatrix(jobs);
    await w.vm.$nextTick();
    // Trigger via the stub's DOM <select> change event — the stub reads
    // selectedOptions and emits update:value, which JobMatrix turns into
    // localStorage.setItem via its watcher.
    const native = w.find('.metric-select-stub');
    Array.from(native.element.querySelectorAll('option')).forEach((o: Element) => {
      (o as HTMLOptionElement).selected = (o as HTMLOptionElement).value === 'MAE';
    });
    await native.trigger('change');
    await w.vm.$nextTick();
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    expect(stored).toEqual(['MAE']);
  });
});
