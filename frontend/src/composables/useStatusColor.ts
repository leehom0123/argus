/**
 * useStatusColor — entity-aware status → colour resolver (#125).
 *
 * The card surface across the app speaks one visual language: a left
 * border tint encodes whether the entity is running / done / failed /
 * stalled / pending. Different entity types have different status
 * vocabularies though — a Host doesn't have a ``failed`` field but it
 * does have ``online`` / ``offline`` / ``stale``; a Project has no
 * single status but rolls up from counters; a Job adds ``is_idle_flagged``
 * which forces it onto the stalled bucket regardless of ``status``.
 *
 * This composable is the single entry point that knows those quirks. It
 * delegates the raw colour table to ``utils/status.ts`` (so the hex
 * palette stays locked in one file) and only adds the per-entity
 * vocabulary translation here.
 *
 * Usage:
 *
 * ```ts
 * import { getStatusColor } from '@/composables/useStatusColor';
 *
 * const tokens = getStatusColor('host', 'online');
 * // tokens.border  → '#52c41a'
 * // tokens.tag     → 'green'
 * // tokens.bucket  → 'running'
 * // tokens.aria    → 'Status: Running'
 * ```
 */

import {
  STATUS_BUCKET_HEX,
  statusBorderColor,
  statusBucket,
  hostAggregateStatus,
  projectAggregateStatus,
  type StatusBucket,
} from '../utils/status';
import type { HostSummary, Batch, ProjectSummary } from '../types';

/**
 * The five colour tokens. Each bucket carries a raw hex (for borders /
 * dots) and the matching ant-design tag preset (for ``<a-tag :color>``).
 *
 * AntD note: ``warning`` and ``orange`` both map to the same yellow-ish
 * preset family. We pick ``warning`` for stalled because that's what
 * StatusTag.vue already used historically and our existing ant-design
 * theme overrides target it.
 */
export const STATUS_COLORS = {
  running: {
    bucket: 'running' as const,
    border: STATUS_BUCKET_HEX.running, // #52c41a
    tag: 'green',
    label: 'Running',
  },
  failed: {
    bucket: 'failed' as const,
    border: STATUS_BUCKET_HEX.failed, // #ff4d4f
    tag: 'red',
    label: 'Failed',
  },
  stalled: {
    bucket: 'stalled' as const,
    border: STATUS_BUCKET_HEX.stalled, // #faad14
    tag: 'warning',
    label: 'Stalled',
  },
  pending: {
    bucket: 'pending' as const,
    border: STATUS_BUCKET_HEX.pending, // #1677ff
    tag: 'blue',
    label: 'Pending',
  },
  done: {
    bucket: 'done' as const,
    border: STATUS_BUCKET_HEX.done, // #d9d9d9
    tag: 'default',
    label: 'Done',
  },
} as const;

/** Public token shape. Hex / tag / label use widened ``string`` types so
 * callers don't trip over the readonly literal types from ``as const``
 * when interpolating into templates. */
export interface StatusColorTokens {
  bucket: StatusBucket;
  /** Hex border colour, or ``'transparent'`` for unknown statuses. */
  border: string;
  /** Ant Design tag preset name (``green`` / ``red`` / ``warning`` / …). */
  tag: string;
  /** Human-readable English label, used as the aria suffix. */
  label: string;
  /** "Status: Running" — a11y label for screen readers. */
  aria: string;
}

/**
 * Default fallback tokens for unknown / missing statuses. Matches the
 * "transparent border" behaviour of ``statusBorderColor`` but with all
 * the other fields filled in so callers don't have to null-check.
 */
const DEFAULT_TOKENS: StatusColorTokens = {
  bucket: 'done',
  border: 'transparent',
  tag: 'default',
  label: 'Unknown',
  aria: 'Status: Unknown',
};

export type StatusEntity = 'project' | 'host' | 'batch' | 'job';

/** Extra context the resolver may need (per entity). */
export interface StatusExtras {
  /** Job: when true, force the ``stalled`` bucket regardless of status. */
  isIdleFlagged?: boolean | null;
  /** Project: optional batch list to roll up via worstStatus(). */
  batches?: Array<Pick<Batch, 'status'>> | null;
  /** Project: counters from ProjectSummary used as a rollup fallback. */
  runningBatches?: number | null;
  jobsFailed?: number | null;
  totalBatches?: number | null;
  /** Host: full HostSummary for ``hostAggregateStatus`` to consume. */
  host?: HostSummary | null;
}

/**
 * Translate an entity-specific status vocabulary into the canonical
 * 5-bucket scheme.
 *
 * - **batch**: status is already canonical (running / done / failed /
 *   stalled / pending / divergent / stopping / stopped / requested).
 * - **job**: same shape as batch BUT ``isIdleFlagged`` overrides to
 *   stalled. JobStatus's ``skipped`` snaps to done (terminal, neutral).
 * - **host**: vocabulary is online / offline / stale. Online → running,
 *   offline → failed, stale → stalled. Pass ``extras.host`` and we'll
 *   roll up via ``hostAggregateStatus`` instead.
 * - **project**: ProjectSummary has no single status. We derive from
 *   ``extras.batches`` (preferred) or counters: runningBatches > 0 →
 *   running; jobsFailed > 0 → failed; totalBatches > 0 → done; else
 *   pending.
 */
export function getStatusColor(
  entity: StatusEntity,
  status: string | null | undefined,
  extras: StatusExtras = {},
): StatusColorTokens {
  const bucket = resolveBucket(entity, status, extras);
  if (!bucket) return DEFAULT_TOKENS;
  const base = STATUS_COLORS[bucket];
  return {
    ...base,
    aria: `Status: ${base.label}`,
  };
}

/**
 * Convenience: just the hex border colour. Equivalent to
 * ``getStatusColor(entity, status, extras).border`` but returns
 * ``'transparent'`` for unknowns to match ``statusBorderColor``.
 */
export function getStatusBorder(
  entity: StatusEntity,
  status: string | null | undefined,
  extras: StatusExtras = {},
): string {
  return getStatusColor(entity, status, extras).border;
}

// ---------------------------------------------------------------------------
// Per-entity bucket resolution. Kept private — callers go through
// getStatusColor / getStatusBorder.
// ---------------------------------------------------------------------------
function resolveBucket(
  entity: StatusEntity,
  status: string | null | undefined,
  extras: StatusExtras,
): StatusBucket | null {
  switch (entity) {
    case 'job':
      // Idle flag forces stalled regardless of underlying status.
      if (extras.isIdleFlagged) return 'stalled';
      if (!status) return null;
      if (status.toLowerCase() === 'skipped') return 'done';
      return statusBucket(status);

    case 'batch':
      return statusBucket(status);

    case 'host':
      // If the caller passed the full host, prefer the aggregate rollup
      // so the border reflects the worst child batch.
      if (extras.host) {
        const agg = hostAggregateStatus(extras.host);
        if (agg) return statusBucket(agg);
      }
      return resolveHostStatus(status);

    case 'project':
      return resolveProjectStatus(status, extras);

    default:
      return statusBucket(status);
  }
}

function resolveHostStatus(
  status: string | null | undefined,
): StatusBucket | null {
  if (!status) return null;
  switch (status.toLowerCase()) {
    case 'online':
    case 'up':
    case 'running':
      return 'running';
    case 'offline':
    case 'down':
    case 'failed':
      return 'failed';
    case 'stale':
    case 'stalled':
      return 'stalled';
    case 'pending':
    case 'connecting':
      return 'pending';
    case 'idle':
    case 'done':
      return 'done';
    default:
      return statusBucket(status);
  }
}

function resolveProjectStatus(
  status: string | null | undefined,
  extras: StatusExtras,
): StatusBucket | null {
  // Caller passed an explicit derived status — honour it.
  if (status) {
    const direct = statusBucket(status);
    if (direct) return direct;
  }

  // Roll up from a recent_batches list when present.
  if (extras.batches && extras.batches.length > 0) {
    const worst = projectAggregateStatus(extras.batches);
    const bucket = statusBucket(worst);
    if (bucket) return bucket;
  }

  // Counter fallback. Severity order: failed > running > done.
  // Rationale: a project with both running batches AND recent failures
  // should pulse red (operator intervention needed), not green. Matches
  // STATUS_PRIORITY in utils/status.ts where failed has rank 0.
  const running = extras.runningBatches ?? 0;
  const failed = extras.jobsFailed ?? 0;
  const total = extras.totalBatches ?? 0;
  if (failed > 0) return 'failed';
  if (running > 0) return 'running';
  if (total > 0) return 'done';
  return null;
}

/**
 * Re-export the legacy hex helper so consumers that already import from
 * ``utils/status`` can switch to this composable in one go without
 * touching their import surface.
 */
export { statusBorderColor };

/**
 * Roll up a ProjectSummary into a single status string. Convenience for
 * card components that only have a ProjectSummary in scope.
 */
export function projectStatusFromSummary(p: ProjectSummary): string {
  // Severity order matches resolveProjectStatus: failed > running > done.
  if ((p.jobs_failed ?? 0) > 0) return 'failed';
  if ((p.running_batches ?? 0) > 0) return 'running';
  if ((p.total_batches ?? p.n_batches ?? 0) > 0) return 'done';
  return '';
}
