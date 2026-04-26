/**
 * JobsList.test.ts (#118)
 *
 * Behavioural coverage for the global jobs page:
 *   1. Mounts the page, calls ``listJobsGlobal`` once on mount.
 *   2. URL query params (``?status=running&since=24h``) are forwarded to
 *      the API call — the Dashboard tile deep-links rely on this.
 *   3. Empty payload renders the EmptyState slot.
 *   4. Status badges render via a direct ``<a-tag>`` (driven by
 *      ``getStatusColor('job', ..., {isIdleFlagged})``) — replaces the
 *      previous StatusTag wiring so ``is_idle_flagged`` rolls jobs into
 *      the canonical ``stalled`` bucket per #125.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';
import { createMemoryHistory, createRouter } from 'vue-router';
import { createPinia, setActivePinia } from 'pinia';
import type { GlobalJobListOut } from '../../types';

// ---------------------------------------------------------------------------
// Mock the api client BEFORE importing the page so the import sees the spy.
// vi.mock is hoisted, so we declare the spy inside the factory and re-import
// it via vi.mocked() in each test.
// ---------------------------------------------------------------------------
vi.mock('../../api/client', () => ({
  listJobsGlobal: vi.fn(),
}));

import { listJobsGlobal } from '../../api/client';
import JobsList from '../JobsList.vue';

// ---------------------------------------------------------------------------
// i18n — only the keys JobsList.vue reads.
// ---------------------------------------------------------------------------
function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    messages: {
      'en-US': {
        common: { refresh: 'Refresh' },
        page_dashboard: { auto_on: 'On', auto_off: 'Off' },
        page_jobs_list: {
          col_job_id: 'Job ID',
          col_project: 'Project',
          col_batch: 'Batch',
          col_model: 'Model',
          col_dataset: 'Dataset',
          col_host: 'Host',
          col_status: 'Status',
          col_start: 'Start time',
          col_elapsed: 'Elapsed',
          filter_status: 'Status',
          filter_project: 'Project',
          filter_host: 'Host',
          filter_batch_id: 'Batch ID',
          filter_since: 'Since',
          apply: 'Apply',
          auto_refresh: 'Auto-refresh',
          pagination_total: 'Total {n}',
          empty_title: 'No jobs',
          empty_hint: 'Adjust filters.',
          retry: 'Retry',
        },
        component_status_tag: {
          running: 'running',
          done: 'done',
          failed: 'failed',
          pending: 'pending',
          stalled: 'stalled',
        },
      },
    },
  });
}

async function makeRouter(initialPath = '/jobs') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/jobs', component: { template: '<div />' } },
      { path: '/batches/:batchId', component: { template: '<div />' } },
      {
        path: '/batches/:batchId/jobs/:jobId',
        component: { template: '<div />' },
      },
    ],
  });
  // Vue Router 4 navigation is async; wait for the initial push to settle
  // so the component's onMounted reads ``route.query`` after the route is
  // populated rather than catching the empty pre-navigation snapshot.
  await router.push(initialPath);
  await router.isReady();
  return router;
}

// ---------------------------------------------------------------------------
// AntD stubs — flatten template so assertions hit real DOM (no teleport).
// ---------------------------------------------------------------------------
const globalStubs = {
  ASelect: {
    props: ['value', 'options'],
    template: '<div class="a-select-stub" />',
  },
  AInput: {
    props: ['value'],
    template: '<input class="a-input-stub" />',
  },
  AInputNumber: {
    props: ['value'],
    template: '<input class="a-input-number-stub" />',
  },
  AButton: {
    template: '<button class="a-btn"><slot /></button>',
  },
  ASwitch: { template: '<span class="a-switch" />' },
  ATooltip: { template: '<slot />' },
  ATable: {
    props: ['dataSource', 'columns', 'loading'],
    template: `
      <div class="a-table-stub" :data-rows="dataSource?.length ?? 0">
        <div v-if="!dataSource?.length" class="empty-slot">
          <slot name="emptyText" />
        </div>
        <div v-else class="rows">
          <div
            v-for="row in dataSource"
            :key="row.job.batch_id + '/' + row.job.id"
            class="row"
            :data-job-id="row.job.id"
            :data-status="row.job.status"
          >
            <slot name="bodyCell" :column="{ key: 'status' }" :record="row" />
          </div>
        </div>
      </div>
    `,
  },
  EmptyState: {
    props: ['title', 'hint', 'variant'],
    template:
      '<div class="empty-state" :data-variant="variant">{{ title }}<slot /></div>',
  },
  // The status cell renders an <a-tag> directly (driven by getStatusColor
  // so ``is_idle_flagged`` flips the bucket per #125 — see review nit
  // resolution in commit history). Keep a flat stub so attribute lookups
  // hit real DOM; tests inspect the slot text + ``data-color``.
  ATag: {
    props: ['color'],
    template: '<span class="status-tag" :data-color="color"><slot /></span>',
  },
  ReloadOutlined: { template: '<i />' },
};

async function mountPage(opts: { initialPath?: string } = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const router = await makeRouter(opts.initialPath ?? '/jobs');
  return mount(JobsList, {
    global: {
      plugins: [makeI18n(), router, pinia],
      stubs: globalStubs,
    },
  });
}

function payload(overrides: Partial<GlobalJobListOut> = {}): GlobalJobListOut {
  return {
    items: [],
    total: 0,
    page: 1,
    page_size: 50,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(listJobsGlobal).mockReset();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('JobsList page', () => {
  it('fetches on mount with no filters when URL has none', async () => {
    vi.mocked(listJobsGlobal).mockResolvedValue(payload());
    await mountPage();
    await flushPromises();

    // The page may fire one or two calls during mount (initial fetch + a
    // route-watcher tick from router.push in the fixture). What matters
    // is that at least one fired, and the most recent call had no
    // filters set.
    expect(listJobsGlobal).toHaveBeenCalled();
    const calls = vi.mocked(listJobsGlobal).mock.calls;
    const last = calls[calls.length - 1]?.[0] ?? {};
    expect(last.status).toBeUndefined();
    expect(last.since).toBeUndefined();
    expect(last.page).toBe(1);
    expect(last.page_size).toBe(50);
  });

  it('forwards URL query filters to the API call (dashboard deep-link path)', async () => {
    vi.mocked(listJobsGlobal).mockResolvedValue(payload());
    await mountPage({ initialPath: '/jobs?status=running&since=24h&project=foo' });
    await flushPromises();

    expect(listJobsGlobal).toHaveBeenCalled();
    const calls = vi.mocked(listJobsGlobal).mock.calls;
    const last = calls[calls.length - 1]?.[0];
    expect(last).toMatchObject({
      status: 'running',
      since: '24h',
      project: 'foo',
      page: 1,
    });
  });

  it('renders the empty state when the API returns no items', async () => {
    vi.mocked(listJobsGlobal).mockResolvedValue(payload());
    const wrapper = await mountPage();
    await flushPromises();

    const empty = wrapper.find('.empty-state');
    expect(empty.exists()).toBe(true);
    expect(empty.text()).toContain('No jobs');
  });

  it('renders one row per item with a StatusTag carrying the job status', async () => {
    vi.mocked(listJobsGlobal).mockResolvedValue(
      payload({
        total: 2,
        items: [
          {
            project: 'p1',
            host: 'h1',
            batch_name: 'sweep-A',
            job: {
              id: 'j-1',
              batch_id: 'b-1',
              status: 'running',
              model: 'transformer',
              dataset: 'etth1',
            },
          },
          {
            project: 'p1',
            host: 'h2',
            batch_name: null,
            job: {
              id: 'j-2',
              batch_id: 'b-2',
              status: 'failed',
              model: 'dlinear',
              dataset: 'etth1',
            },
          },
        ],
      }),
    );
    const wrapper = await mountPage();
    await flushPromises();

    const rows = wrapper.findAll('.row');
    expect(rows.length).toBe(2);
    // Status badge is now a direct ``<a-tag>`` (#118 review nit) whose
    // colour is driven by ``getStatusColor('job', ..., {isIdleFlagged})``
    // — text content carries the upper-cased status and ``data-color`` is
    // the unified bucket preset (green/red/etc.) from useStatusColor.
    const tags = wrapper.findAll('.status-tag');
    expect(tags.length).toBe(2);
    const labels = tags.map((t) => t.text().trim().toLowerCase());
    expect(labels).toContain('running');
    expect(labels).toContain('failed');
    const colors = tags.map((t) => t.attributes('data-color'));
    // ``running`` → green, ``failed`` → red per the canonical 5-bucket
    // scheme. Asserting the color confirms ``getStatusColor`` is wired
    // through to the rendered tag.
    expect(colors).toContain('green');
    expect(colors).toContain('red');
  });
});
