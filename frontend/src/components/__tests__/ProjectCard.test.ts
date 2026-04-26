/**
 * ProjectCard.test.ts
 *
 * Pins the v0.1.3 density extension on ``ProjectCard.vue``:
 *   1. Brand row renders the project name + first owner chip with "+N" suffix.
 *   2. Health strip exposes the new "Fail %" cell when ``failure_rate`` is set.
 *   3. Winner row renders the top model × dataset + metric.
 *   4. Trend row renders the GPU-hours label and the 7-day sparkline mount.
 *   5. Graceful degradation: rows hide when their backing data is missing.
 *
 * Heavily stubs Ant Design + child components so the assertions hit real
 * template output without dragging in echarts / popovers / router resolution.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';
import { createMemoryHistory, createRouter } from 'vue-router';
import ProjectCard from '../ProjectCard.vue';
import type { ProjectSummary } from '../../types';

// ---------------------------------------------------------------------------
// Minimal i18n — only the keys ProjectCard.vue reads.
// ---------------------------------------------------------------------------
function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    messages: {
      'en-US': {
        component_project_card: {
          no_events: 'No events yet',
          last_event: 'last event {time}',
          label_running: 'Running',
          label_batches: 'Batches',
          label_failed_jobs: 'Failed jobs',
          best: 'best {label}',
          eta: 'ETA {duration}',
          running_ellipsis: 'running',
          failed_count: '{count} failed',
          fail_rate: 'Fail %',
          top_models: 'Top models',
          batches_per_week: '{n} batches / 7d',
          gpu_hrs_unit: 'GPU-hrs',
        },
      },
    },
  });
}

// ---------------------------------------------------------------------------
// A pass-through router so useRouter() doesn't blow up. Routes are
// irrelevant here because the card's @click is never triggered in tests.
// ---------------------------------------------------------------------------
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/projects/:p', component: { template: '<div />' } },
      { path: '/demo/projects/:p', component: { template: '<div />' } },
    ],
  });
}

// ---------------------------------------------------------------------------
// Stubs — keep template output flat and assertable.
// ---------------------------------------------------------------------------
const globalStubs = {
  ACard: {
    inheritAttrs: false,
    template: '<div class="a-card"><slot /></div>',
  },
  ATooltip: { template: '<slot />' },
  StarButton: { template: '<button data-testid="star" />' },
  MiniSparkline: {
    props: ['data', 'height', 'color', 'area'],
    template: '<div data-testid="sparkline" :data-len="data?.length ?? 0" />',
  },
  // Icon components used in ProjectCard.vue.
  FieldTimeOutlined: { template: '<span />' },
  AimOutlined: { template: '<span />' },
  ExperimentOutlined: { template: '<span />' },
  WarningFilled: { template: '<span />' },
  TrophyOutlined: { template: '<span data-testid="trophy" />' },
  ThunderboltOutlined: { template: '<span data-testid="thunder" />' },
  TeamOutlined: { template: '<span data-testid="team" />' },
};

function makeProject(overrides: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    project: 'demo-proj',
    n_batches: 12,
    running_batches: 2,
    jobs_done: 30,
    jobs_failed: 5,
    last_event_at: '2026-04-25T08:00:00Z',
    is_starred: false,
    ...overrides,
  };
}

function mountCard(project: ProjectSummary) {
  return mount(ProjectCard, {
    global: {
      plugins: [makeI18n(), makeRouter()],
      stubs: globalStubs,
    },
    props: { project },
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('ProjectCard density rows', () => {
  it('renders brand row with first owner + "+N" suffix when multi-owner', () => {
    const wrapper = mountCard(
      makeProject({ owners: ['alice', 'bob', 'carol'] }),
    );
    const html = wrapper.html();
    expect(html).toContain('demo-proj');
    expect(html).toContain('alice +2');
  });

  it('omits owner chip when no owners are listed', () => {
    const wrapper = mountCard(makeProject({ owners: [] }));
    expect(wrapper.find('[data-testid="team"]').exists()).toBe(false);
  });

  it('renders Fail % cell only when failure_rate is provided', () => {
    const withRate = mountCard(makeProject({ failure_rate: 0.18 }));
    expect(withRate.html()).toContain('Fail %');
    expect(withRate.html()).toContain('18%');

    const without = mountCard(makeProject({ failure_rate: undefined }));
    expect(without.html()).not.toContain('Fail %');
  });

  it('renders the winner row with top model × dataset + metric', () => {
    const wrapper = mountCard(
      makeProject({
        top_models: [
          {
            model: 'patchtst',
            dataset: 'etth1',
            metric_name: 'MSE',
            metric_value: 0.3215,
          },
        ],
      }),
    );
    const html = wrapper.html();
    expect(wrapper.find('[data-testid="trophy"]').exists()).toBe(true);
    expect(html).toContain('Top models');
    expect(html).toContain('patchtst');
    expect(html).toContain('etth1');
    expect(html).toContain('MSE');
    expect(html).toContain('0.3215');
  });

  it('hides the winner row when top_models is empty', () => {
    const wrapper = mountCard(makeProject({ top_models: [] }));
    expect(wrapper.find('[data-testid="trophy"]').exists()).toBe(false);
  });

  it('renders the trend row with sparkline + GPU-hours when both present', () => {
    const wrapper = mountCard(
      makeProject({
        batch_volume_7d: [1, 0, 2, 3, 1, 4, 5],
        gpu_hours: 12.7,
      }),
    );
    const spark = wrapper.find('[data-testid="sparkline"]');
    expect(spark.exists()).toBe(true);
    expect(spark.attributes('data-len')).toBe('7');
    const html = wrapper.html();
    expect(html).toContain('16 batches / 7d'); // 1+0+2+3+1+4+5
    expect(html).toContain('12.7');
    expect(html).toContain('GPU-hrs');
  });

  it('hides the GPU-hours chip when gpu_hours is zero', () => {
    const wrapper = mountCard(
      makeProject({ batch_volume_7d: [0, 0, 0, 0, 0, 0, 0], gpu_hours: 0 }),
    );
    // Sparkline still renders (length-7 array), but no GPU-hrs label.
    expect(wrapper.find('[data-testid="thunder"]').exists()).toBe(false);
  });

  it('hides the trend row entirely when no sparkline + no gpu hours', () => {
    const wrapper = mountCard(makeProject({}));
    expect(wrapper.find('[data-testid="sparkline"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="thunder"]').exists()).toBe(false);
  });

  it('renders all four density rows together when fully populated', () => {
    const wrapper = mountCard(
      makeProject({
        owners: ['alice'],
        failure_rate: 0.22,
        top_models: [
          {
            model: 'dlinear',
            dataset: 'etth2',
            metric_name: 'MSE',
            metric_value: 0.45,
          },
        ],
        batch_volume_7d: [2, 3, 1, 0, 4, 5, 2],
        gpu_hours: 8.4,
      }),
    );
    const html = wrapper.html();
    // Brand
    expect(html).toContain('alice');
    // Fail %
    expect(html).toContain('Fail %');
    expect(html).toContain('22%');
    // Winner
    expect(html).toContain('dlinear');
    // Trend
    expect(wrapper.find('[data-testid="sparkline"]').exists()).toBe(true);
    expect(html).toContain('GPU-hrs');
  });
});
