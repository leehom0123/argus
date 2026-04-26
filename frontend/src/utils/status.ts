/**
 * Shared status → colour helpers.
 *
 * **One source of truth** for how a Batch / Project / Host / Job status maps
 * to a user-facing colour. Every card component, table row, and detail
 * header routes through {@link statusBorderColor} so the visual language
 * stays consistent: the left border encodes status, the inside stays
 * neutral.
 *
 * The 5-colour scheme (locked in #125 — "unify status colours across cards"):
 *
 *   running     #52c41a  green       active execution (jobs running, batches
 *                                    in flight, hosts with running_jobs > 0)
 *   failed      #ff4d4f  red         hard failure / error
 *   stalled     #faad14  yellow/gold heartbeat lost, divergence, or other
 *                                    "warn but still alive" states
 *   pending     #1677ff  blue        waiting / queued / requested rerun
 *   done        #d9d9d9  gray        finished, ordinary card — unobtrusive
 *
 * Plus three legacy aliases that snap onto the 5-colour buckets:
 *   - ``stopping`` / ``stopped``   → ``done`` family (gray)
 *   - ``divergent``                → ``stalled`` family (yellow — quality warning)
 *   - ``requested``                → ``pending`` family (blue — waiting on a host)
 *   - ``success`` / ``completed``  → ``done`` family (gray)
 *   - ``error``                    → ``failed`` family (red)
 *   - ``in_progress``              → ``running`` family (green)
 *
 * Tags (StatusTag.vue) use ant-design presets ("blue", "green", "warning",
 * …) instead of raw hex because Ant picks light + dark variants for its
 * backgrounds. Borders are raw hex because we draw them ourselves. Both
 * routes share the same logical bucket so a "running" tag and a "running"
 * border always agree.
 */

import type { HostSummary, Batch, BatchStatus } from '../types';

/**
 * The five locked hex tokens. Card borders, dots, and tints derive from
 * these so the palette can be tweaked in exactly one place. Anything that
 * reaches for a status colour must route through here — no hard-coded
 * hexes scattered across components.
 */
export const STATUS_BUCKET_HEX = {
  running: '#52c41a', // green  — active
  failed: '#ff4d4f', // red    — bad
  stalled: '#faad14', // yellow — warning / heartbeat lost
  pending: '#1677ff', // blue   — queued / waiting
  done: '#d9d9d9', // gray   — ordinary / finished
} as const;

export type StatusBucket = keyof typeof STATUS_BUCKET_HEX;

/**
 * Per-status border hex. Includes legacy aliases that snap onto the
 * five-colour buckets so existing API status strings keep working. The
 * full alias list is enumerated explicitly (rather than computed) so a
 * grep for any concrete status string lands on this file.
 */
export const STATUS_BORDER_COLORS = {
  // running family (green)
  running: STATUS_BUCKET_HEX.running,
  in_progress: STATUS_BUCKET_HEX.running,
  // failed family (red)
  failed: STATUS_BUCKET_HEX.failed,
  error: STATUS_BUCKET_HEX.failed,
  // stalled / warning family (yellow)
  stalled: STATUS_BUCKET_HEX.stalled,
  divergent: STATUS_BUCKET_HEX.stalled,
  // pending family (blue)
  pending: STATUS_BUCKET_HEX.pending,
  queued: STATUS_BUCKET_HEX.pending,
  requested: STATUS_BUCKET_HEX.pending,
  // done family (gray)
  done: STATUS_BUCKET_HEX.done,
  success: STATUS_BUCKET_HEX.done,
  completed: STATUS_BUCKET_HEX.done,
  stopping: STATUS_BUCKET_HEX.done,
  stopped: STATUS_BUCKET_HEX.done,
} as const;

/**
 * Map a status string to its semantic bucket. Unknown statuses resolve to
 * ``null`` so callers can decide whether to render anything at all.
 */
export function statusBucket(
  status: string | null | undefined,
): StatusBucket | null {
  if (!status) return null;
  const key = status.toLowerCase();
  switch (key) {
    case 'running':
    case 'in_progress':
      return 'running';
    case 'failed':
    case 'error':
      return 'failed';
    case 'stalled':
    case 'divergent':
      return 'stalled';
    case 'pending':
    case 'queued':
    case 'requested':
      return 'pending';
    case 'done':
    case 'success':
    case 'completed':
    case 'stopping':
    case 'stopped':
      return 'done';
    default:
      return null;
  }
}

/**
 * Return the hex border colour for a status string. Unknown / missing
 * statuses resolve to ``'transparent'`` so the caller doesn't have to
 * special-case anything — paint a 4 px left border unconditionally.
 */
export function statusBorderColor(status: string | null | undefined): string {
  if (!status) return 'transparent';
  const key = status.toLowerCase() as keyof typeof STATUS_BORDER_COLORS;
  return STATUS_BORDER_COLORS[key] ?? 'transparent';
}

/**
 * Priority order (lower number = more urgent / more dominant).
 *
 * Used by {@link hostAggregateStatus} and project-level aggregations where
 * we have many child statuses and need to pick ONE representative colour.
 * Failed dominates everything; warning states (stalled / divergent) come
 * next; then in-flight (running / stopping); then waiting (requested);
 * then terminal (stopped / done).
 */
const STATUS_PRIORITY: Record<string, number> = {
  failed: 0,
  divergent: 1,
  stalled: 2,
  stopping: 3,
  running: 4,
  requested: 5,
  pending: 5,
  queued: 5,
  stopped: 6,
  done: 7,
};

/**
 * Pick the most-urgent status from a list. Unknown statuses sort to the
 * end; if the input is empty we return an empty string so callers can
 * pass the result straight through {@link statusBorderColor} and get
 * ``transparent`` back.
 */
export function worstStatus(statuses: (string | null | undefined)[]): string {
  let best: string = '';
  let bestRank = Number.POSITIVE_INFINITY;
  for (const raw of statuses) {
    if (!raw) continue;
    const s = raw.toLowerCase();
    const rank = STATUS_PRIORITY[s] ?? 99;
    if (rank < bestRank) {
      best = s;
      bestRank = rank;
    }
  }
  return best;
}

/**
 * Derive a single status string for a host from its list of currently-
 * running (or recently-completed) batches.
 *
 * Contract with the backend / HostSummary:
 *   - ``host.batches`` (if present) is the authoritative list — a future
 *     BE tweak can add it without a breaking change.
 *   - When absent, we fall back to ``host.warnings`` heuristics: a
 *     non-empty warnings list implies some kind of failure state; an
 *     active ``running_jobs`` count implies "running"; else transparent.
 *
 * This keeps the function useful today (before BE adds host.batches) and
 * forward-compatible tomorrow.
 */
export function hostAggregateStatus(host: HostSummary): string {
  const batches = (host as HostSummary & { batches?: Batch[] }).batches;
  if (batches && batches.length > 0) {
    return worstStatus(batches.map((b) => b.status as string | null));
  }
  // Heuristic fallback.
  if ((host.warnings ?? []).length > 0) return 'failed';
  if ((host.running_jobs ?? 0) > 0) return 'running';
  return '';
}

/**
 * Convenience: same idea as {@link hostAggregateStatus} but for an array
 * of Batch rows. Used by ProjectCard when the API returns recent_batches
 * but no pre-computed project health field.
 */
export function projectAggregateStatus(
  batches: Array<Pick<Batch, 'status'>> | null | undefined,
): string {
  if (!batches || batches.length === 0) return '';
  return worstStatus(batches.map((b) => b.status as string | null));
}

/**
 * Re-export for StatusTag.vue and other consumers that want to know the
 * canonical status list without importing the type system. Keeps the
 * colour palette + the allowed status strings in lockstep — if you add
 * a status here, add it to ``types.ts`` BatchStatus too.
 */
export type KnownStatus =
  | BatchStatus
  | 'stalled'
  | 'requested'
  | 'stopped'
  | 'queued';
