/**
 * JobDetail.test.ts
 *
 * Pins the #104 layout refactor:
 *   1. The sticky telemetry strip mounts with all 5 cells (status,
 *      elapsed/eta, gpu_util, gpu_mem, latest loss).
 *   2. The log tail mounts inline (LogTailPanel), not as a drawer —
 *      asserted by the absence of any ``a-drawer`` element and the
 *      presence of the ``log-tail-panel`` data-test marker.
 *   3. On md viewport (< 992px) the two-column layout collapses to a
 *      tab group — asserted by the ``job-detail-tabs`` marker showing
 *      and the side-by-side row not rendering.
 *
 * The API client and SSE EventSource are stubbed so the test runs
 * fully offline; the test only cares about layout / mount semantics,
 * not data flow.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';
import { createRouter, createMemoryHistory } from 'vue-router';
import { nextTick } from 'vue';

// Stub the API client BEFORE importing JobDetail so the module-level
// imports resolve to vi-stubbed exports.
vi.mock('../../api/client', () => {
  return {
    getJob: vi.fn().mockResolvedValue({
      id: 'job-1',
      batch_id: 'batch-1',
      model: 'transformer',
      dataset: 'etth1',
      status: 'running',
      start_time: '2026-04-25T10:00:00Z',
      end_time: null,
      elapsed_s: 1234,
      metrics: { MSE: 0.123 },
      run_dir: '/runs/job-1',
      is_idle_flagged: false,
      extra: { command: 'python main.py model=transformer' },
    }),
    getJobEpochs: vi.fn().mockResolvedValue([
      { epoch: 1, train_loss: 0.5, val_loss: 0.4, lr: 1e-4 },
      { epoch: 2, train_loss: 0.4, val_loss: 0.35, lr: 1e-4 },
    ]),
    getJobEta: vi.fn().mockResolvedValue({ eta_s: 600, eta_iso: '2026-04-25T11:00:00Z' }),
    getResources: vi.fn().mockResolvedValue([
      {
        timestamp: '2026-04-25T10:01:00Z',
        gpu_util_pct: 87,
        gpu_mem_mb: 8192,
        gpu_mem_total_mb: 24576,
      },
    ]),
    deleteJob: vi.fn().mockResolvedValue(undefined),
  };
});

// Stub usePermissions so canWrite is true and isAnonymous is false.
vi.mock('../../composables/usePermissions', () => ({
  usePermissions: () => ({ canWrite: { value: true }, isAnonymous: { value: false } }),
}));

// Stub LossChart — defineAsyncComponent + echarts pull in heavy modules
// we don't want in jsdom. The empty stub below renders synchronously.
// We add ``__isTeleport: false`` because vue-test-utils' default-stub
// path probes the resolved async component for that internal flag and
// the strict mock proxy throws if it's missing.
vi.mock('../../components/LossChart.vue', () => ({
  default: {
    name: 'LossChartStub',
    __isTeleport: false,
    __isKeepAlive: false,
    template: '<div class="loss-chart-stub" />',
  },
}));

// Stub MetricsBar (uses useChart → echarts in jsdom).
vi.mock('../../components/MetricsBar.vue', () => ({
  default: {
    name: 'MetricsBarStub',
    props: ['metrics'],
    template: '<div class="metrics-bar-stub" />',
  },
}));

// Stub ShareDialog (pulls share-state hooks we don't exercise here).
vi.mock('../../components/ShareDialog.vue', () => ({
  default: {
    name: 'ShareDialogStub',
    props: ['open', 'batchId'],
    emits: ['update:open', 'changed'],
    template: '<div class="share-dialog-stub" />',
  },
}));

// Stub LogTailPanel — we only assert its presence, not its internals
// (those have separate tests in __tests__/LogTailPanel if needed).
vi.mock('../../components/LogTailPanel.vue', () => ({
  default: {
    name: 'LogTailPanelStub',
    props: ['batchId', 'jobId', 'height'],
    template: '<div class="log-tail-panel-stub" data-test="log-tail-panel" />',
  },
}));

import JobDetail from '../JobDetail.vue';

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    messages: {
      'en-US': {
        common: { confirm_delete_job: 'Delete?', delete: 'Delete', cancel: 'Cancel' },
        component_eta: {
          warming_up: 'warming up',
          eta_label: 'ETA',
          hover_finish_at: 'Finish ~{time}',
        },
        page_job_detail: {
          back_to_batch: 'Batch',
          desc_job_id: 'Job ID',
          desc_batch_id: 'Batch ID',
          desc_model: 'Model',
          desc_dataset: 'Dataset',
          desc_status: 'Status',
          desc_elapsed: 'Elapsed',
          desc_start: 'Start',
          desc_end: 'End',
          desc_run_dir: 'Run dir',
          card_loss_curve: 'Loss curve',
          loss_empty: 'no curve',
          card_final_metrics: 'Final metrics',
          metrics_empty: 'no metrics',
          card_logs: 'Logs',
          tab_curve: 'Curve',
          tab_logs: 'Logs',
          telemetry: {
            status: 'Status',
            elapsed_eta: 'Elapsed / ETA',
            eta_short: 'ETA',
            gpu_util: 'GPU util',
            gpu_mem_peak: 'GPU mem peak',
            latest_loss: 'Loss (latest)',
          },
          actions: {
            stop: 'Stop',
            rerun: 'Rerun',
            share: 'Share',
            copy_command: 'Copy command',
            copied: 'Copied',
            copy_failed: 'Copy failed',
          },
        },
      },
    },
  });
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/batches/:batchId', component: { template: '<div />' } },
      { path: '/demo/batches/:batchId', component: { template: '<div />' } },
    ],
  });
}

function setViewport(width: number) {
  // jsdom doesn't move on resize but we can stub matchMedia so the
  // component picks up the right breakpoint at mount time.
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: matchesQuery(query, width),
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  });
}

function matchesQuery(query: string, width: number): boolean {
  // Naive parser — handles only ``(min-width: NNNpx)``, which is all
  // JobDetail uses today.
  const m = query.match(/min-width:\s*(\d+)px/);
  if (!m) return false;
  return width >= parseInt(m[1], 10);
}

async function mountJobDetail(width = 1440) {
  setViewport(width);
  const router = makeRouter();
  const i18n = makeI18n();

  const wrapper = mount(JobDetail, {
    props: { batchId: 'batch-1', jobId: 'job-1' },
    global: {
      plugins: [i18n, router],
      stubs: {
        // The page wraps LossChart in defineAsyncComponent. The async
        // wrapper confuses vue-test-utils' default-stub path (it probes
        // ``__isTeleport`` on the resolved module). Stubbing by display
        // name short-circuits the wrapper and avoids the warning.
        LossChart: { name: 'LossChartStub', template: '<div class="loss-chart-stub" />' },
        AsyncComponentWrapper: { name: 'AsyncStub', template: '<div class="async-stub" />' },
        // Trim AntD components down to lightweight DOM so class assertions
        // hit real elements and we avoid teleports.
        ATooltip: { template: '<div class="tooltip-stub"><slot /></div>' },
        APopconfirm: { template: '<div class="popconfirm-stub"><slot /></div>' },
        ATag: { template: '<span class="tag-stub"><slot /></span>' },
        AButton: { template: '<button class="btn-stub"><slot /></button>' },
        ACard: {
          props: ['title'],
          template: '<div class="card-stub"><div class="card-title-stub">{{ title }}</div><slot /></div>',
        },
        ARow: { template: '<div class="row-stub"><slot /></div>' },
        ACol: { template: '<div class="col-stub"><slot /></div>' },
        ADescriptions: { template: '<dl class="descriptions-stub"><slot /></dl>' },
        ADescriptionsItem: {
          props: ['label'],
          template: '<div class="desc-item-stub"><dt>{{ label }}</dt><dd><slot /></dd></div>',
        },
        ATabs: { template: '<div class="tabs-stub" data-test="job-detail-tabs"><slot /></div>' },
        ATabPane: {
          props: ['tab'],
          template: '<div class="tab-pane-stub" :data-tab="tab"><slot /></div>',
        },
        ACollapse: { template: '<div class="collapse-stub"><slot /></div>' },
        ACollapsePanel: {
          props: ['header'],
          template: '<div class="collapse-panel-stub"><div class="header">{{ header }}</div><slot /></div>',
        },
        StatusTag: { props: ['status'], template: '<span class="status-tag-stub">{{ status }}</span>' },
        JobIdleBadge: { template: '<span class="idle-badge-stub" />' },
        AnonymousCTA: { template: '<div class="anon-cta-stub" />' },
      },
    },
  });

  await flushPromises();
  await nextTick();
  return wrapper;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('JobDetail layout refactor (#104)', () => {
  beforeEach(() => {
    // Clipboard API isn't in jsdom by default; stub for copy tests.
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('mounts the telemetry strip with 5 cells on lg viewport', async () => {
    const w = await mountJobDetail(1440);
    const strip = w.find('[data-test="telemetry-strip"]');
    expect(strip.exists()).toBe(true);

    // Each cell has a unique data-test marker.
    expect(w.find('[data-test="telemetry-status"]').exists()).toBe(true);
    expect(w.find('[data-test="telemetry-elapsed"]').exists()).toBe(true);
    expect(w.find('[data-test="telemetry-gpu-util"]').exists()).toBe(true);
    expect(w.find('[data-test="telemetry-gpu-mem"]').exists()).toBe(true);
    expect(w.find('[data-test="telemetry-loss"]').exists()).toBe(true);

    // 5 children inside the strip.
    expect(strip.findAll('.telemetry-cell').length).toBe(5);
  });

  it('renders the log tail inline (no drawer wrapper)', async () => {
    const w = await mountJobDetail(1440);
    // The inline panel marker must be present.
    expect(w.find('[data-test="log-tail-panel"]').exists()).toBe(true);
    // No <a-drawer> is rendered — the drawer wrapper is deliberately gone.
    expect(w.find('a-drawer').exists()).toBe(false);
    expect(w.find('.ant-drawer').exists()).toBe(false);
    // No "open logs drawer" button — logs always visible.
    const html = w.html();
    expect(html.toLowerCase()).not.toContain('open logs drawer');
  });

  it('mounts the bottom action bar with stop / rerun / share / copy', async () => {
    const w = await mountJobDetail(1440);
    const bar = w.find('[data-test="action-bar"]');
    expect(bar.exists()).toBe(true);
    const text = bar.text();
    expect(text).toContain('Stop');
    expect(text).toContain('Rerun');
    expect(text).toContain('Share');
    expect(text).toContain('Copy command');
  });

  it('collapses to tabs on md viewport (< 992px)', async () => {
    const w = await mountJobDetail(800);
    expect(w.find('[data-test="job-detail-tabs"]').exists()).toBe(true);
    // Telemetry strip is still there.
    expect(w.find('[data-test="telemetry-strip"]').exists()).toBe(true);
    // Log tail panel is still mounted (inside the Logs tab).
    expect(w.find('[data-test="log-tail-panel"]').exists()).toBe(true);
  });

  it('does not mount tabs on lg+ viewport (uses two-column row)', async () => {
    const w = await mountJobDetail(1440);
    expect(w.find('[data-test="job-detail-tabs"]').exists()).toBe(false);
    // Two-column layout = a row with at least 2 cols rendering.
    const cols = w.findAll('.col-stub');
    expect(cols.length).toBeGreaterThanOrEqual(2);
    // Log tail panel marker must be present (inline, not in a tab).
    expect(w.find('[data-test="log-tail-panel"]').exists()).toBe(true);
  });
});
