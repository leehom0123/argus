/**
 * status-color.test.ts
 *
 * Pins the unified 5-colour Batch / Project / Host / Job status → border
 * hex map locked in #125. Drift is the #1 source of visual inconsistency
 * across cards, so we assert the full table here once and route every
 * consumer (StatusTag.vue, BatchCard, BatchCompactCard, ProjectCard,
 * HostCard, HostList table rows, BatchDetail header, JobMatrix) through
 * the same helper.
 *
 * 5-colour scheme:
 *   running  → #52c41a (green)   — active execution
 *   failed   → #ff4d4f (red)     — hard error
 *   stalled  → #faad14 (yellow)  — heartbeat lost / divergent
 *   pending  → #1677ff (blue)    — queued / waiting / requested
 *   done     → #d9d9d9 (gray)    — finished, ordinary card
 *
 * If you change a hex in ``utils/status.ts`` without updating this test
 * (or vice versa) you break PM's design spec — ask first.
 *
 * @vitest-environment node
 */
import { describe, it, expect } from 'vitest';

import {
  STATUS_BORDER_COLORS,
  STATUS_BUCKET_HEX,
  hostAggregateStatus,
  projectAggregateStatus,
  statusBorderColor,
  statusBucket,
  worstStatus,
} from '../utils/status';

describe('STATUS_BUCKET_HEX (5-colour scheme)', () => {
  it('locks the five canonical hex tokens', () => {
    expect(STATUS_BUCKET_HEX.running).toBe('#52c41a'); // green
    expect(STATUS_BUCKET_HEX.failed).toBe('#ff4d4f'); // red
    expect(STATUS_BUCKET_HEX.stalled).toBe('#faad14'); // yellow
    expect(STATUS_BUCKET_HEX.pending).toBe('#1677ff'); // blue
    expect(STATUS_BUCKET_HEX.done).toBe('#d9d9d9'); // gray
  });
});

describe('statusBorderColor()', () => {
  it('returns green for running family', () => {
    expect(statusBorderColor('running')).toBe('#52c41a');
    expect(statusBorderColor('in_progress')).toBe('#52c41a');
  });

  it('returns red for failed family', () => {
    expect(statusBorderColor('failed')).toBe('#ff4d4f');
    expect(statusBorderColor('error')).toBe('#ff4d4f');
  });

  it('returns yellow for stalled / divergent (warning) family', () => {
    expect(statusBorderColor('stalled')).toBe('#faad14');
    expect(statusBorderColor('divergent')).toBe('#faad14');
  });

  it('returns blue for pending / queued / requested family', () => {
    expect(statusBorderColor('pending')).toBe('#1677ff');
    expect(statusBorderColor('queued')).toBe('#1677ff');
    expect(statusBorderColor('requested')).toBe('#1677ff');
  });

  it('returns gray for done / completed / stopped family', () => {
    expect(statusBorderColor('done')).toBe('#d9d9d9');
    expect(statusBorderColor('success')).toBe('#d9d9d9');
    expect(statusBorderColor('completed')).toBe('#d9d9d9');
    expect(statusBorderColor('stopping')).toBe('#d9d9d9');
    expect(statusBorderColor('stopped')).toBe('#d9d9d9');
  });

  it('is case-insensitive', () => {
    expect(statusBorderColor('RUNNING')).toBe('#52c41a');
    expect(statusBorderColor('Done')).toBe('#d9d9d9');
    expect(statusBorderColor('STALLED')).toBe('#faad14');
  });

  it('returns transparent for unknown / empty / null', () => {
    expect(statusBorderColor(null)).toBe('transparent');
    expect(statusBorderColor(undefined)).toBe('transparent');
    expect(statusBorderColor('')).toBe('transparent');
    expect(statusBorderColor('skipped')).toBe('transparent');
    expect(statusBorderColor('totally-bogus')).toBe('transparent');
  });

  it('exports the palette as a const for downstream consumers', () => {
    expect(STATUS_BORDER_COLORS.stalled).toBe('#faad14');
    expect(STATUS_BORDER_COLORS.running).toBe('#52c41a');
    expect(STATUS_BORDER_COLORS.pending).toBe('#1677ff');
    expect(STATUS_BORDER_COLORS.done).toBe('#d9d9d9');
  });
});

describe('statusBucket()', () => {
  it('groups aliases into the five buckets', () => {
    expect(statusBucket('running')).toBe('running');
    expect(statusBucket('in_progress')).toBe('running');
    expect(statusBucket('failed')).toBe('failed');
    expect(statusBucket('error')).toBe('failed');
    expect(statusBucket('stalled')).toBe('stalled');
    expect(statusBucket('divergent')).toBe('stalled');
    expect(statusBucket('pending')).toBe('pending');
    expect(statusBucket('queued')).toBe('pending');
    expect(statusBucket('requested')).toBe('pending');
    expect(statusBucket('done')).toBe('done');
    expect(statusBucket('success')).toBe('done');
    expect(statusBucket('completed')).toBe('done');
    expect(statusBucket('stopping')).toBe('done');
    expect(statusBucket('stopped')).toBe('done');
  });

  it('returns null for unknown / empty', () => {
    expect(statusBucket(null)).toBeNull();
    expect(statusBucket(undefined)).toBeNull();
    expect(statusBucket('')).toBeNull();
    expect(statusBucket('skipped')).toBeNull();
    expect(statusBucket('garbage')).toBeNull();
  });
});

describe('worstStatus()', () => {
  it('picks failed over divergent over stalled over running', () => {
    expect(worstStatus(['running', 'divergent', 'failed'])).toBe('failed');
    expect(worstStatus(['running', 'divergent'])).toBe('divergent');
    expect(worstStatus(['stalled', 'running'])).toBe('stalled');
    expect(worstStatus(['running', 'done'])).toBe('running');
    expect(worstStatus(['done', 'done'])).toBe('done');
  });

  it('ignores null / undefined entries', () => {
    expect(worstStatus([null, 'running', undefined])).toBe('running');
  });

  it('returns empty string on empty input', () => {
    expect(worstStatus([])).toBe('');
    expect(worstStatus([null, undefined, ''])).toBe('');
  });

  it('routes cleanly into statusBorderColor', () => {
    const w = worstStatus(['running', 'stalled', 'done']);
    expect(statusBorderColor(w)).toBe('#faad14');
  });
});

describe('hostAggregateStatus()', () => {
  it('derives from host.batches when present', () => {
    const host = {
      host: 'gpu-1',
      batches: [
        { status: 'running' as const },
        { status: 'divergent' as const },
      ],
    };
    expect(hostAggregateStatus(host as never)).toBe('divergent');
  });

  it('falls back to warnings → failed', () => {
    expect(
      hostAggregateStatus({
        host: 'gpu-2',
        warnings: ['GPU 95°C'],
      } as never),
    ).toBe('failed');
  });

  it('falls back to running_jobs > 0 → running', () => {
    expect(
      hostAggregateStatus({
        host: 'gpu-3',
        running_jobs: 2,
      } as never),
    ).toBe('running');
  });

  it('returns empty string for idle hosts', () => {
    expect(
      hostAggregateStatus({
        host: 'gpu-idle',
        running_jobs: 0,
      } as never),
    ).toBe('');
  });
});

describe('projectAggregateStatus()', () => {
  it('returns empty for null / empty', () => {
    expect(projectAggregateStatus(null)).toBe('');
    expect(projectAggregateStatus([])).toBe('');
  });

  it('picks the worst across recent batches', () => {
    expect(
      projectAggregateStatus([
        { status: 'running' },
        { status: 'failed' },
        { status: 'done' },
      ] as never),
    ).toBe('failed');
  });
});
