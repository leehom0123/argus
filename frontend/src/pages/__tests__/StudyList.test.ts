/**
 * StudyList.test.ts (v0.2 hyperopt-ui)
 *
 * Behavioural coverage for the Optuna studies overview page:
 *   1. Mounts the page, calls ``listStudies`` once on mount.
 *   2. Empty payload renders the EmptyState slot with the expected variant
 *      (``empty_studies``) so the hint catalog can override the message.
 *   3. Populated payload renders one row per study with the headline value
 *      and ``best_metric`` tag wired through.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';
import { createMemoryHistory, createRouter } from 'vue-router';
import { createPinia, setActivePinia } from 'pinia';
import type { StudyListOut, StudySummary } from '../../api/studies';

// ---------------------------------------------------------------------------
// Mock the api module BEFORE importing the page so the import sees the spy.
// vi.mock is hoisted, so the spy lives inside the factory and is re-imported
// via vi.mocked() in each test.
// ---------------------------------------------------------------------------
vi.mock('../../api/studies', () => ({
  listStudies: vi.fn(),
}));

import { listStudies } from '../../api/studies';
import StudyList from '../StudyList.vue';

// ---------------------------------------------------------------------------
// i18n — only the keys StudyList.vue reads.
// ---------------------------------------------------------------------------
function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    messages: {
      'en-US': {
        common: { refresh: 'Refresh' },
        page_studies: {
          title: 'Optuna Studies',
          col_name: 'Study name',
          col_n_trials: 'Trials',
          col_best: 'Best value',
          col_direction: 'Direction',
          col_sampler: 'Sampler',
          col_last_run: 'Last run',
          failed: 'failed',
          empty_title: 'No Optuna studies yet',
          empty_hint: 'Run a Hydra multirun to see trials here.',
        },
      },
    },
  });
}

async function makeRouter(initialPath = '/studies') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/studies', component: { template: '<div />' } },
      { path: '/studies/:name', component: { template: '<div />' } },
    ],
  });
  await router.push(initialPath);
  await router.isReady();
  return router;
}

// ---------------------------------------------------------------------------
// AntD stubs — same flatten pattern as JobsList.test.ts so DOM assertions
// hit real elements rather than teleported portals.
// ---------------------------------------------------------------------------
const globalStubs = {
  AButton: {
    template: '<button class="a-btn"><slot /></button>',
  },
  ATable: {
    props: ['dataSource', 'loading'],
    template: `
      <div class="a-table-stub" :data-rows="dataSource?.length ?? 0">
        <div v-if="!dataSource?.length" class="empty-slot"><slot name="emptyText" /></div>
        <div v-else class="rows">
          <div
            v-for="row in dataSource"
            :key="row.study_name"
            class="row"
            :data-study="row.study_name"
            :data-best="row.best_value"
            :data-metric="row.best_metric"
          />
        </div>
      </div>
    `,
  },
  ATableColumn: { template: '<div class="a-tcol"><slot /></div>' },
  ATag: {
    props: ['color'],
    template: '<span class="a-tag" :data-color="color"><slot /></span>',
  },
  EmptyState: {
    props: ['title', 'hint', 'variant'],
    template:
      '<div class="empty-state" :data-variant="variant">{{ title }}<slot /></div>',
  },
  ReloadOutlined: { template: '<i />' },
  ExperimentOutlined: { template: '<i />' },
};

async function mountPage() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const router = await makeRouter();
  return mount(StudyList, {
    global: {
      plugins: [makeI18n(), router, pinia],
      stubs: globalStubs,
    },
  });
}

function studySummary(overrides: Partial<StudySummary> = {}): StudySummary {
  return {
    study_name: 'study_x',
    n_trials: 0,
    n_done: 0,
    n_failed: 0,
    best_value: null,
    best_metric: null,
    direction: null,
    sampler: null,
    last_run: null,
    ...overrides,
  };
}

function payload(studies: StudySummary[]): StudyListOut {
  return { studies };
}

beforeEach(() => {
  vi.mocked(listStudies).mockReset();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('StudyList page', () => {
  it('calls listStudies once on mount', async () => {
    vi.mocked(listStudies).mockResolvedValue(payload([]));
    await mountPage();
    await flushPromises();

    expect(listStudies).toHaveBeenCalledTimes(1);
  });

  it('renders the empty state with variant=empty_studies when there are no studies', async () => {
    vi.mocked(listStudies).mockResolvedValue(payload([]));
    const wrapper = await mountPage();
    await flushPromises();

    const empty = wrapper.find('.empty-state');
    expect(empty.exists()).toBe(true);
    expect(empty.attributes('data-variant')).toBe('empty_studies');
    // The i18n title bubbles up untouched.
    expect(empty.text()).toContain('No Optuna studies yet');
  });

  it('renders one row per study with best_value + best_metric wired through', async () => {
    vi.mocked(listStudies).mockResolvedValue(
      payload([
        studySummary({
          study_name: 'dam_forecast_optimization',
          n_trials: 12,
          n_done: 10,
          n_failed: 2,
          best_value: 0.182,
          best_metric: 'MSE',
          direction: 'minimize',
          sampler: 'TPESampler',
        }),
        studySummary({
          study_name: 'study_beta',
          n_trials: 5,
          n_done: 5,
          best_value: 0.31,
          best_metric: 'MSE',
          direction: 'minimize',
        }),
      ]),
    );
    const wrapper = await mountPage();
    await flushPromises();

    const rows = wrapper.findAll('.row');
    expect(rows.length).toBe(2);

    // Row order from API is preserved (FE doesn't re-sort).
    const names = rows.map((r) => r.attributes('data-study'));
    expect(names).toEqual(['dam_forecast_optimization', 'study_beta']);

    // best_value / best_metric attributes survive the round-trip.
    expect(rows[0].attributes('data-best')).toBe('0.182');
    expect(rows[0].attributes('data-metric')).toBe('MSE');
  });
});
