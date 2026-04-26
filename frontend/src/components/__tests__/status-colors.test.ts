/**
 * status-colors.test.ts
 *
 * Pins the unified 5-colour scheme on two axes (#125):
 *   1. ``getStatusColor(entity, status, extras?)`` returns the correct
 *      bucket / border hex / AntD tag preset / aria-label across all
 *      entity × status combinations (project / host / batch / job).
 *   2. The card components (ProjectCard / HostCard / BatchCard) render
 *      the matching border colour and ``aria-label`` for a given input.
 *
 * Sister to ``src/__tests__/status-color.test.ts`` which pins the raw
 * hex palette in ``utils/status.ts``. This file focuses on the
 * entity-aware resolver and the surface that consumes it.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';
import { createMemoryHistory, createRouter } from 'vue-router';

import {
  STATUS_COLORS,
  getStatusColor,
  getStatusBorder,
  projectStatusFromSummary,
} from '../../composables/useStatusColor';
import ProjectCard from '../ProjectCard.vue';
import HostCard from '../HostCard.vue';
import BatchCard from '../BatchCard.vue';
import type {
  ProjectSummary,
  HostSummary,
  ActiveBatchCard,
} from '../../types';

// ---------------------------------------------------------------------------
// 1. STATUS_COLORS map — the 5-bucket source of truth.
// ---------------------------------------------------------------------------
describe('STATUS_COLORS map (5-colour scheme)', () => {
  it('exposes the five canonical buckets with locked tokens', () => {
    expect(STATUS_COLORS.running).toMatchObject({
      bucket: 'running',
      border: '#52c41a',
      tag: 'green',
    });
    expect(STATUS_COLORS.failed).toMatchObject({
      bucket: 'failed',
      border: '#ff4d4f',
      tag: 'red',
    });
    expect(STATUS_COLORS.stalled).toMatchObject({
      bucket: 'stalled',
      border: '#faad14',
      tag: 'warning',
    });
    expect(STATUS_COLORS.pending).toMatchObject({
      bucket: 'pending',
      border: '#1677ff',
      tag: 'blue',
    });
    expect(STATUS_COLORS.done).toMatchObject({
      bucket: 'done',
      border: '#d9d9d9',
      tag: 'default',
    });
  });
});

// ---------------------------------------------------------------------------
// 2. getStatusColor — per-entity resolution.
// ---------------------------------------------------------------------------
describe('getStatusColor — batch entity', () => {
  it.each([
    ['running', 'running', '#52c41a'],
    ['done', 'done', '#d9d9d9'],
    ['failed', 'failed', '#ff4d4f'],
    ['stalled', 'stalled', '#faad14'],
    ['divergent', 'stalled', '#faad14'],
    ['pending', 'pending', '#1677ff'],
    ['queued', 'pending', '#1677ff'],
    ['requested', 'pending', '#1677ff'],
    ['stopping', 'done', '#d9d9d9'],
    ['stopped', 'done', '#d9d9d9'],
    ['success', 'done', '#d9d9d9'],
    ['error', 'failed', '#ff4d4f'],
  ])('maps batch.%s → bucket=%s border=%s', (status, bucket, border) => {
    const tokens = getStatusColor('batch', status);
    expect(tokens.bucket).toBe(bucket);
    expect(tokens.border).toBe(border);
  });

  it('returns transparent border + default fallback for unknown', () => {
    const tokens = getStatusColor('batch', 'totally-bogus');
    expect(tokens.border).toBe('transparent');
    expect(tokens.tag).toBe('default');
    expect(tokens.aria).toBe('Status: Unknown');
  });

  it('attaches an ARIA label per bucket', () => {
    expect(getStatusColor('batch', 'running').aria).toBe('Status: Running');
    expect(getStatusColor('batch', 'failed').aria).toBe('Status: Failed');
    expect(getStatusColor('batch', 'stalled').aria).toBe('Status: Stalled');
    expect(getStatusColor('batch', 'pending').aria).toBe('Status: Pending');
    expect(getStatusColor('batch', 'done').aria).toBe('Status: Done');
  });
});

describe('getStatusColor — host entity', () => {
  it('maps online/offline/stale to running/failed/stalled', () => {
    expect(getStatusColor('host', 'online').bucket).toBe('running');
    expect(getStatusColor('host', 'offline').bucket).toBe('failed');
    expect(getStatusColor('host', 'stale').bucket).toBe('stalled');
  });

  it('falls through to the canonical bucket for batch-style strings', () => {
    expect(getStatusColor('host', 'running').bucket).toBe('running');
    expect(getStatusColor('host', 'failed').bucket).toBe('failed');
  });

  it('rolls up via hostAggregateStatus when host extra is provided', () => {
    const host: Partial<HostSummary> & { batches?: { status: string }[] } = {
      host: 'gpu-1',
      batches: [{ status: 'running' }, { status: 'failed' }],
    };
    const tokens = getStatusColor('host', null, {
      host: host as never,
    });
    // failed wins via worstStatus; bucket → failed.
    expect(tokens.bucket).toBe('failed');
    expect(tokens.border).toBe('#ff4d4f');
  });

  it('returns transparent for an idle host with no extras', () => {
    expect(getStatusColor('host', null).border).toBe('transparent');
  });
});

describe('getStatusColor — project entity', () => {
  it('honours an explicit derived status string when provided', () => {
    expect(getStatusColor('project', 'running').bucket).toBe('running');
    expect(getStatusColor('project', 'failed').bucket).toBe('failed');
    expect(getStatusColor('project', 'done').bucket).toBe('done');
  });

  it('rolls up from extras.batches when status is empty', () => {
    const tokens = getStatusColor('project', '', {
      batches: [{ status: 'failed' }, { status: 'running' }] as never,
    });
    expect(tokens.bucket).toBe('failed');
  });

  it('counter rollup: runningBatches > 0 with no failures → running', () => {
    expect(
      getStatusColor('project', '', {
        runningBatches: 3,
        jobsFailed: 0,
        totalBatches: 10,
      }).bucket,
    ).toBe('running');
  });

  it('counter rollup: jobsFailed > 0, no running → failed', () => {
    expect(
      getStatusColor('project', '', {
        runningBatches: 0,
        jobsFailed: 2,
        totalBatches: 10,
      }).bucket,
    ).toBe('failed');
  });

  it('counter rollup: failed dominates running (severity order)', () => {
    // A project with both running batches and recent failures should
    // pulse red — operator intervention takes priority over showing
    // healthy progress. Matches STATUS_PRIORITY where failed=0 (highest).
    expect(
      getStatusColor('project', '', {
        runningBatches: 3,
        jobsFailed: 1,
        totalBatches: 10,
      }).bucket,
    ).toBe('failed');
  });

  it('counter rollup: totalBatches > 0, no running / failed → done', () => {
    expect(
      getStatusColor('project', '', {
        runningBatches: 0,
        jobsFailed: 0,
        totalBatches: 4,
      }).bucket,
    ).toBe('done');
  });

  it('returns transparent for an empty project (no batches at all)', () => {
    const tokens = getStatusColor('project', '', {
      runningBatches: 0,
      jobsFailed: 0,
      totalBatches: 0,
    });
    expect(tokens.border).toBe('transparent');
  });
});

describe('getStatusColor — job entity', () => {
  it('maps standard statuses like a batch', () => {
    expect(getStatusColor('job', 'running').bucket).toBe('running');
    expect(getStatusColor('job', 'failed').bucket).toBe('failed');
    expect(getStatusColor('job', 'done').bucket).toBe('done');
    expect(getStatusColor('job', 'pending').bucket).toBe('pending');
  });

  it('isIdleFlagged forces stalled regardless of status', () => {
    expect(
      getStatusColor('job', 'running', { isIdleFlagged: true }).bucket,
    ).toBe('stalled');
    expect(
      getStatusColor('job', 'pending', { isIdleFlagged: true }).bucket,
    ).toBe('stalled');
  });

  it('skipped → done (terminal, neutral)', () => {
    expect(getStatusColor('job', 'skipped').bucket).toBe('done');
  });
});

describe('getStatusBorder convenience', () => {
  it('returns just the hex string', () => {
    expect(getStatusBorder('batch', 'running')).toBe('#52c41a');
    expect(getStatusBorder('batch', 'failed')).toBe('#ff4d4f');
    expect(getStatusBorder('host', 'stale')).toBe('#faad14');
    expect(getStatusBorder('job', 'pending')).toBe('#1677ff');
    expect(getStatusBorder('project', '', { totalBatches: 5 })).toBe('#d9d9d9');
  });
});

describe('projectStatusFromSummary', () => {
  it('reads ProjectSummary counters', () => {
    expect(
      projectStatusFromSummary({
        project: 'a',
        running_batches: 1,
      } as ProjectSummary),
    ).toBe('running');
    expect(
      projectStatusFromSummary({
        project: 'a',
        running_batches: 0,
        jobs_failed: 2,
      } as ProjectSummary),
    ).toBe('failed');
    expect(
      projectStatusFromSummary({
        project: 'a',
        running_batches: 0,
        jobs_failed: 0,
        n_batches: 5,
      } as ProjectSummary),
    ).toBe('done');
    expect(
      projectStatusFromSummary({
        project: 'a',
      } as ProjectSummary),
    ).toBe('');
  });

  it('failed dominates running (severity order)', () => {
    expect(
      projectStatusFromSummary({
        project: 'a',
        running_batches: 3,
        jobs_failed: 1,
      } as ProjectSummary),
    ).toBe('failed');
  });
});

// ---------------------------------------------------------------------------
// 3. Card render assertions — each card surfaces the right border + aria.
// ---------------------------------------------------------------------------
function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    missingWarn: false,
    fallbackWarn: false,
    messages: {
      'en-US': {
        // Loose passthrough: tests don't assert on exact translated text,
        // only on border / aria, so empty objects are fine.
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
        component_host_card: {
          gpu_temp_c: '{c}°C',
          jobs_running: '{count} running',
          disk_free: '{size} free',
          running_jobs_top5_label: 'Top jobs',
        },
        component_batch_card: {
          stalled: 'Stalled',
          elapsed: 'elapsed {duration}',
          eta: 'ETA {duration}',
          running_label: '{count} running',
          disk_free: '{size} free',
          failed_count: '{count} failed',
          btn_matrix: 'Matrix',
          btn_jobs: 'Jobs',
          btn_share: 'Share',
        },
        component_status_tag: { unknown: 'UNKNOWN', status_done: 'DONE' },
      },
    },
  });
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/projects/:p', component: { template: '<div />' } },
      { path: '/demo/projects/:p', component: { template: '<div />' } },
      { path: '/hosts/:h', component: { template: '<div />' } },
      { path: '/batches/:b', component: { template: '<div />' } },
    ],
  });
}

/**
 * AntCard stub — passes ``style`` and ``aria-label`` through onto the
 * rendered DOM root so the assertions can read both off the wrapper.
 */
const ACardStub = {
  inheritAttrs: false,
  props: ['size', 'hoverable', 'bodyStyle'],
  template: `
    <div class="a-card" :style="$attrs.style" :aria-label="$attrs['aria-label']" :data-status-bucket="$attrs['data-status-bucket']">
      <slot />
    </div>`,
};

const sharedStubs = {
  ACard: ACardStub,
  ATag: { template: '<span><slot /></span>' },
  ATooltip: { template: '<slot />' },
  AProgress: { template: '<div class="a-progress" />' },
  AButton: { template: '<button><slot /></button>' },
  StarButton: { template: '<button data-testid="star" />' },
  PinButton: { template: '<button data-testid="pin" />' },
  StatusTag: { props: ['status'], template: '<span :data-status="status" />' },
  ProgressInline: { template: '<div class="progress-inline" />' },
  MiniSparkline: {
    props: ['data', 'height', 'color', 'area'],
    template: '<div data-testid="sparkline" :data-len="data?.length ?? 0" />',
  },
  // Icons used across cards.
  FieldTimeOutlined: { template: '<span />' },
  AimOutlined: { template: '<span />' },
  ExperimentOutlined: { template: '<span />' },
  WarningFilled: { template: '<span />' },
  TrophyOutlined: { template: '<span />' },
  ThunderboltOutlined: { template: '<span />' },
  TeamOutlined: { template: '<span />' },
  UserOutlined: { template: '<span />' },
  DatabaseOutlined: { template: '<span />' },
  ClockCircleFilled: { template: '<span />' },
  CloseCircleFilled: { template: '<span />' },
  ShareAltOutlined: { template: '<span />' },
  ArrowDownOutlined: { template: '<span />' },
  ArrowUpOutlined: { template: '<span />' },
  MinusOutlined: { template: '<span />' },
};

function mountCard<T extends object>(Comp: unknown, props: T) {
  return mount(Comp as never, {
    global: {
      plugins: [makeI18n(), makeRouter()],
      stubs: sharedStubs,
    },
    props: props as never,
  });
}

describe('ProjectCard renders the right border + aria', () => {
  function project(overrides: Partial<ProjectSummary> = {}): ProjectSummary {
    return {
      project: 'demo',
      n_batches: 0,
      running_batches: 0,
      jobs_done: 0,
      jobs_failed: 0,
      last_event_at: null,
      is_starred: false,
      ...overrides,
    };
  }

  it('running project → green border + aria=Running', () => {
    const w = mountCard(ProjectCard, { project: project({ running_batches: 1 }) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#52c41a|rgb\(82,\s*196,\s*26\)/);
    expect(card.attributes('aria-label')).toBe('Status: Running');
    expect(card.attributes('data-status-bucket')).toBe('running');
  });

  it('failed-jobs project → red border + aria=Failed', () => {
    const w = mountCard(ProjectCard, {
      project: project({ jobs_failed: 5, n_batches: 4 }),
    });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#ff4d4f|rgb\(255,\s*77,\s*79\)/);
    expect(card.attributes('aria-label')).toBe('Status: Failed');
  });

  it('finished project (only done batches) → gray border + aria=Done', () => {
    const w = mountCard(ProjectCard, { project: project({ n_batches: 3 }) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#d9d9d9|rgb\(217,\s*217,\s*217\)/);
    expect(card.attributes('aria-label')).toBe('Status: Done');
  });

  it('empty project → transparent border', () => {
    const w = mountCard(ProjectCard, { project: project({}) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toContain('transparent');
  });
});

describe('HostCard renders the right border + aria', () => {
  function host(overrides: Partial<HostSummary> = {}): HostSummary {
    return {
      host: 'gpu-1',
      gpu_util_pct: 0,
      ram_mb: 0,
      ram_total_mb: 16000,
      disk_free_mb: 50_000,
      disk_total_mb: 100_000,
      running_jobs: 0,
      ...overrides,
    };
  }

  it('host with running jobs → green border + aria=Running', () => {
    const w = mountCard(HostCard, { host: host({ running_jobs: 2 }) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#52c41a|rgb\(82,\s*196,\s*26\)/);
    expect(card.attributes('aria-label')).toBe('Status: Running');
  });

  it('host with warnings → red border + aria=Failed', () => {
    const w = mountCard(HostCard, {
      host: host({ warnings: ['GPU 95°C'], running_jobs: 0 }),
    });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#ff4d4f|rgb\(255,\s*77,\s*79\)/);
    expect(card.attributes('aria-label')).toBe('Status: Failed');
  });

  it('idle host → transparent border', () => {
    const w = mountCard(HostCard, { host: host({}) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toContain('transparent');
  });
});

describe('BatchCard renders the right border + aria', () => {
  function batch(overrides: Partial<ActiveBatchCard> = {}): ActiveBatchCard {
    return {
      batch_id: 'b1',
      project: 'demo',
      status: 'running',
      n_total: 10,
      n_done: 3,
      n_failed: 0,
      ...overrides,
    };
  }

  it('running → green border + aria=Running', () => {
    const w = mountCard(BatchCard, { data: batch({ status: 'running' }) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#52c41a|rgb\(82,\s*196,\s*26\)/);
    expect(card.attributes('aria-label')).toBe('Status: Running');
  });

  it('failed → red border + aria=Failed', () => {
    const w = mountCard(BatchCard, { data: batch({ status: 'failed' }) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#ff4d4f|rgb\(255,\s*77,\s*79\)/);
    expect(card.attributes('aria-label')).toBe('Status: Failed');
  });

  it('stalled → yellow border + aria=Stalled', () => {
    const w = mountCard(BatchCard, { data: batch({ status: 'stalled' }) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#faad14|rgb\(250,\s*173,\s*20\)/);
    expect(card.attributes('aria-label')).toBe('Status: Stalled');
  });

  it('pending → blue border + aria=Pending', () => {
    const w = mountCard(BatchCard, { data: batch({ status: 'pending' }) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#1677ff|rgb\(22,\s*119,\s*255\)/);
    expect(card.attributes('aria-label')).toBe('Status: Pending');
  });

  it('done → gray border + aria=Done', () => {
    const w = mountCard(BatchCard, { data: batch({ status: 'done' }) });
    const card = w.find('.a-card');
    expect(card.attributes('style')).toMatch(/#d9d9d9|rgb\(217,\s*217,\s*217\)/);
    expect(card.attributes('aria-label')).toBe('Status: Done');
  });

  it('is_stalled flag overrides aria to Stalled even when status=running', () => {
    const w = mountCard(BatchCard, {
      data: batch({ status: 'running', is_stalled: true }),
    });
    const card = w.find('.a-card');
    // border still tracks data.status (running → green) for back-compat,
    // but the aria label flips so screen-readers announce the stalled state.
    expect(card.attributes('aria-label')).toBe('Status: Stalled');
  });
});
