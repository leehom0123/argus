import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios';
import { notification } from 'ant-design-vue';
import type {
  Batch,
  Job,
  EpochPoint,
  ResourceSnapshot,
  ListBatchesParams,
  ListJobsParams,
  ListResourcesParams,
  GlobalJobListOut,
} from '../types';

export const http = axios.create({
  baseURL: '/api',
  timeout: 10_000,
});

// -------- JWT injection --------
//
// We read the token directly from localStorage each request rather than holding a
// reference to the Pinia store here. Reason: this file is imported by the store
// itself — reaching back into it would create a circular dependency. The store is
// the single writer to these localStorage keys.

const LS_TOKEN_KEY = 'argus.access_token';
const LS_EXPIRES_KEY = 'em_expires_at';

/**
 * Public endpoints must never send a JWT (avoid leaking viewer identity and
 * so the backend can't distinguish anonymous vs stale-session visitors).
 * We match URL paths after the `/api` prefix; both `"/public/xyz"` and
 * `"/api/public/xyz"` forms need to be covered in case callers pass absolute
 * URLs at some point.
 */
function isPublicPath(url?: string): boolean {
  if (!url) return false;
  return url.startsWith('/public/') || url.includes('/api/public/');
}

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  if (isPublicPath(config.url)) return config;
  const token = localStorage.getItem(LS_TOKEN_KEY);
  if (token) {
    config.headers = config.headers ?? {};
    (config.headers as Record<string, string>).Authorization = `Bearer ${token}`;
  }
  return config;
});

// -------- Response error routing --------

/** Error payload shape thrown on 423 Locked. */
export class LoginLockedError extends Error {
  retry_after: number;
  constructor(retry_after: number, message = 'Account locked') {
    super(message);
    this.name = 'LoginLockedError';
    this.retry_after = retry_after;
  }
}

/**
 * Registered handler for session expiry (401). The auth store installs this on
 * startup. We use an indirection to avoid circular imports.
 */
let onUnauthorized: (() => void) | null = null;
export function registerUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

/** Paths where a 401 should NOT trigger a redirect (e.g. /auth/login itself). */
const AUTH_BYPASS_PATHS = [
  '/auth/login',
  '/auth/register',
  '/auth/verify-email',
  '/auth/request-password-reset',
  '/auth/reset-password',
  '/auth/oauth/config',
];

function isAuthBypass(url?: string): boolean {
  if (!url) return false;
  return AUTH_BYPASS_PATHS.some((p) => url.includes(p));
}

let lastErrorAt = 0;
http.interceptors.response.use(
  (resp) => resp,
  (err: AxiosError) => {
    const status = err.response?.status;
    const url = err.config?.url ?? '';

    // 401 — session expired or invalid. Route to login unless this IS a login call.
    if (status === 401 && !isAuthBypass(url)) {
      if (onUnauthorized) {
        try {
          onUnauthorized();
        } catch {
          // swallow — we don't want handler errors to mask the underlying 401
        }
      }
      return Promise.reject(err);
    }

    // 423 Locked — login rate-limit. Surface retry_after to the caller.
    if (status === 423) {
      const retryHeader = err.response?.headers?.['retry-after'];
      const retry = Number(retryHeader) || 600;
      const locked = new LoginLockedError(retry, 'Account temporarily locked');
      return Promise.reject(locked);
    }

    // 403 — forbidden. Always toast; callers can still handle.
    if (status === 403) {
      const now = Date.now();
      if (now - lastErrorAt > 2000) {
        lastErrorAt = now;
        notification.warning({
          message: 'No permission',
          description: 'You do not have access to this resource.',
          duration: 3,
        });
      }
      return Promise.reject(err);
    }

    // Default toast behaviour (throttled) for 404 / 5xx / network.
    const now = Date.now();
    if (now - lastErrorAt > 2000) {
      lastErrorAt = now;
      if (status === 404) {
        notification.warning({
          message: 'Not found',
          description: `${url} returned 404`,
          duration: 3,
        });
      } else if (status && status >= 500) {
        notification.error({
          message: `Server error ${status}`,
          description: `${url} failed. Check backend logs.`,
          duration: 4,
        });
      } else if (!err.response) {
        // Silent: transient network hiccups (SSE reconnects, tab
        // backgrounding, flaky cellular) are self-healing. Let the
        // per-page loading state show "failed to load" instead of
        // spamming a global toast on every blip. Log to console so
        // devs can still triage in production.
        // eslint-disable-next-line no-console
        console.warn('[api] network error (no response)', url, err.message);
      }
    }
    return Promise.reject(err);
  },
);

// -------- typed helpers (data endpoints) --------

export async function listBatches(params: ListBatchesParams = {}): Promise<Batch[]> {
  const { data } = await http.get<Batch[]>('/batches', { params });
  return data;
}

// ---------------------------------------------------------------------------
// Bulk /batches/compact (perf: eliminates N×4 per-card fan-out)
// ---------------------------------------------------------------------------

/** Latest ``job_epoch`` row bundled in the compact response. */
export interface CompactJobEpochLatest {
  job_id: string;
  epoch: number;
  train_loss?: number | null;
  val_loss?: number | null;
  lr?: number | null;
  val_loss_trace: number[];
}

/** Raw snapshot shape from the compact endpoint's ``resources`` array.
 *  Uses the canonical ResourceSnapshotOut field names (not the remapped
 *  ``/batches/{id}/resources`` shape). */
export interface CompactResourceSnapshot {
  id: number;
  host: string;
  timestamp: string;
  gpu_util_pct?: number | null;
  gpu_mem_mb?: number | null;
  gpu_mem_total_mb?: number | null;
  gpu_temp_c?: number | null;
  cpu_util_pct?: number | null;
  ram_mb?: number | null;
  ram_total_mb?: number | null;
  disk_free_mb?: number | null;
}

export interface BatchCompactItem {
  batch: Batch;
  jobs: Job[];
  epochs_latest: CompactJobEpochLatest[];
  resources: CompactResourceSnapshot[];
}

export interface BatchCompactListOut {
  batches: BatchCompactItem[];
}

export interface ListBatchesCompactParams extends ListBatchesParams {
  /** Max resource snapshots returned per batch (1..100, default 20). */
  resource_limit?: number;
}

/**
 * Bulk fetch batch-compact data in one round-trip.
 *
 * Replaces the ``1 + N×4`` fan-out on /batches where each card would
 * GET ``/batches/{id}`` + ``/batches/{id}/jobs`` +
 * ``/batches/{id}/epochs/latest`` + ``/batches/{id}/resources``.
 * Backend answers in 4 queries regardless of N.
 */
export async function getBatchesCompact(
  params: ListBatchesCompactParams = {},
): Promise<BatchCompactListOut> {
  const { data } = await http.get<BatchCompactListOut>('/batches/compact', {
    params,
  });
  return data;
}

export async function getBatch(id: string): Promise<Batch> {
  const { data } = await http.get<Batch>(`/batches/${encodeURIComponent(id)}`);
  return data;
}

export async function listJobs(batchId: string): Promise<Job[]> {
  const { data } = await http.get<Job[]>(`/batches/${encodeURIComponent(batchId)}/jobs`);
  return data;
}

/**
 * Global jobs list — every job the caller can see across batches/hosts.
 * Used by the JobsList page (``/jobs``) and the Dashboard counter tiles
 * which deep-link with a pre-filled status/since.
 */
export async function listJobsGlobal(
  params: ListJobsParams = {},
): Promise<GlobalJobListOut> {
  const { data } = await http.get<GlobalJobListOut>('/jobs', { params });
  return data;
}

/**
 * Rerun an existing batch with hyperparameter overrides.
 *
 * Calls POST /api/batches/{id}/rerun. The backend may still be catching
 * up (Team-Rerun agent lands the endpoint separately); on 404 the axios
 * interceptor will surface a "Not found" toast to the user.
 */
export async function rerunBatch(
  batchId: string,
  overrides: Record<string, unknown>,
  name?: string,
): Promise<{ batch_id: string; name?: string }> {
  const { data } = await http.post<{ batch_id: string; name?: string }>(
    `/batches/${encodeURIComponent(batchId)}/rerun`,
    { overrides, name },
  );
  return data;
}

export async function getJob(batchId: string, jobId: string): Promise<Job> {
  const { data } = await http.get<Job>(
    `/jobs/${encodeURIComponent(batchId)}/${encodeURIComponent(jobId)}`,
  );
  return data;
}

export async function getJobEpochs(batchId: string, jobId: string): Promise<EpochPoint[]> {
  const { data } = await http.get<EpochPoint[]>(
    `/jobs/${encodeURIComponent(batchId)}/${encodeURIComponent(jobId)}/epochs`,
  );
  return data;
}

export async function listHosts(): Promise<string[]> {
  const { data } = await http.get<string[]>('/resources/hosts');
  return data;
}

export async function getResources(params: ListResourcesParams = {}): Promise<ResourceSnapshot[]> {
  const { data } = await http.get<ResourceSnapshot[]>('/resources', { params });
  return data;
}

// ---------------------------------------------------------------------------
// Batch live-panel helpers (resources / log-lines / epoch progress)
// ---------------------------------------------------------------------------

export interface LogLine {
  /** DB row id — present from poll responses, missing (0) from SSE frames. */
  id: number;
  /** Client-generated UUID (v1.1 envelopes); present on SSE frames + poll
   *  responses. Preferred dedup key because it's the only id the SSE wire
   *  carries — see JobDetail.vue::pushLogLine. */
  event_id?: string | null;
  batch_id: string;
  job_id?: string | null;
  timestamp: string;
  level?: string | null;
  message?: string | null;
}

export interface JobEpochLatest {
  job_id: string;
  timestamp: string;
  epoch: number;
  train_loss?: number | null;
  val_loss?: number | null;
  lr?: number | null;
  /** Last ≤20 val_loss values for a mini sparkline. */
  val_loss_trace?: (number | null)[];
}

// Raw snapshot shape from the /batches/{id}/resources endpoint (field names differ from canonical).
interface RawBatchSnapshot {
  ts?: string;
  timestamp?: string;
  gpu_util?: number | null;
  gpu_util_pct?: number | null;
  vram_used_mb?: number | null;
  gpu_mem_mb?: number | null;
  vram_total_mb?: number | null;
  gpu_mem_total_mb?: number | null;
  cpu_util?: number | null;
  cpu_util_pct?: number | null;
  ram_used_mb?: number | null;
  ram_mb?: number | null;
  ram_total_mb?: number | null;
  disk_free_gb?: number | null;
  disk_free_mb?: number | null;
  gpu_temp_c?: number | null;
  extra?: string | Record<string, unknown> | null;
  [key: string]: unknown;
}

/** Recent resource snapshots for the host that ran a batch (newest → oldest). */
export async function getBatchResources(
  batchId: string,
  limit = 120,
): Promise<{ host: string | null; snapshots: ResourceSnapshot[] }> {
  const { data } = await http.get<{ host?: string; snapshots: RawBatchSnapshot[] } | RawBatchSnapshot[]>(
    `/batches/${encodeURIComponent(batchId)}/resources`,
    { params: { limit } },
  );

  let host: string | null = null;
  let rawSnapshots: RawBatchSnapshot[] = [];

  if (data && !Array.isArray(data) && 'snapshots' in data) {
    host = data.host ?? null;
    rawSnapshots = data.snapshots;
  } else {
    rawSnapshots = data as RawBatchSnapshot[];
  }

  // Normalise batch-resource field names to the canonical ResourceSnapshot shape.
  const snapshots: ResourceSnapshot[] = rawSnapshots.map((s) => {
    const extraRaw = s.extra;
    let extraParsed: Record<string, unknown> = {};
    if (typeof extraRaw === 'string') {
      try { extraParsed = JSON.parse(extraRaw); } catch { /* ignore */ }
    } else if (extraRaw && typeof extraRaw === 'object') {
      extraParsed = extraRaw as Record<string, unknown>;
    }
    return {
      timestamp: (s.ts ?? s.timestamp ?? '') as string,
      host: host ?? undefined,
      gpu_util_pct: s.gpu_util_pct ?? s.gpu_util ?? null,
      gpu_mem_mb: s.gpu_mem_mb ?? s.vram_used_mb ?? null,
      gpu_mem_total_mb: s.gpu_mem_total_mb ?? s.vram_total_mb ?? null,
      gpu_temp_c: s.gpu_temp_c ?? (extraParsed.gpu_temp_c as number | null | undefined) ?? null,
      cpu_util_pct: s.cpu_util_pct ?? s.cpu_util ?? null,
      ram_mb: s.ram_mb ?? s.ram_used_mb ?? null,
      ram_total_mb: s.ram_total_mb ?? null,
      // disk_free_gb (new API) × 1024 → MB; fall back to disk_free_mb (old shape).
      disk_free_mb: s.disk_free_mb ?? (s.disk_free_gb != null ? s.disk_free_gb * 1024 : null),
      pid: extraParsed.pid ?? null,
      ...extraParsed,
    } as ResourceSnapshot;
  });

  return { host, snapshots };
}

/** Raw shape the backend actually returns for each log-line row. */
interface RawLogLine {
  ts?: string;
  timestamp?: string;
  job_id?: string | null;
  level?: string | null;
  /** Backend field name is "line", not "message". */
  line?: string | null;
  message?: string | null;
  id?: number | null;
  batch_id?: string | null;
}

/** Last `limit` log_line events for a batch, oldest→newest.
 *
 * The backend returns `{ts, job_id, level, line}` but the LogLine interface
 * (used throughout the template) uses `{timestamp, message}`.  We normalise
 * here so every consumer gets the canonical shape.
 *
 * Default limit bumped 50→200 to match the backend default; healthy INFO-
 * level runs blow through 50 lines in seconds.  Pass `bust=true` to
 * append a cache-busting query param (used by the UI's Refresh button).
 */
export async function getBatchLogLines(
  batchId: string,
  limit = 200,
  bust = false,
): Promise<LogLine[]> {
  const params: Record<string, string | number> = { limit };
  if (bust) params.bust = String(Date.now());
  const { data } = await http.get<RawLogLine[]>(
    `/batches/${encodeURIComponent(batchId)}/log-lines`,
    { params },
  );
  return (data ?? []).map((row, idx) => ({
    id: row.id ?? idx,
    batch_id: row.batch_id ?? batchId,
    job_id: row.job_id ?? null,
    timestamp: row.timestamp ?? row.ts ?? '',
    level: row.level ?? null,
    message: row.message ?? row.line ?? '',
  }));
}

/** Latest epoch data per job for a batch (training-progress panel). */
export async function getBatchEpochsLatest(batchId: string): Promise<JobEpochLatest[]> {
  const { data } = await http.get<{ jobs: JobEpochLatest[] } | JobEpochLatest[]>(
    `/batches/${encodeURIComponent(batchId)}/epochs/latest`,
  );
  // Backend returns {jobs:[...]}; unwrap to flat array for callers.
  if (data && !Array.isArray(data) && 'jobs' in data) {
    return data.jobs;
  }
  return data as JobEpochLatest[];
}

// ---------------------------------------------------------------------------
// Per-job log-lines + live SSE stream
// ---------------------------------------------------------------------------

/** Backend shape for ``GET /api/jobs/{batch}/{job}/log-lines``. */
interface RawJobLogLine {
  id: number;
  event_id: string | null;
  ts: string;
  job_id: string | null;
  level: string;
  line: string;
}

/**
 * Initial-fill / catch-up poll for the JobDetail Logs tab.
 *
 * Returns the most-recent ``limit`` ``log_line`` events for one job,
 * normalised to the same :interface:`LogLine` shape used everywhere
 * else in the UI. ``since`` is the highest event id already in the
 * client buffer — pass it to advance the cursor without re-fetching.
 *
 * Backend caches at 10 s TTL; the SSE stream covers freshness in
 * between, so the cache rarely hurts.
 */
export async function getJobLogLines(
  batchId: string,
  jobId: string,
  limit = 200,
  since?: number,
): Promise<LogLine[]> {
  const params: Record<string, string | number> = { limit };
  if (since !== undefined && since !== null) params.since = since;
  const { data } = await http.get<RawJobLogLine[]>(
    `/jobs/${encodeURIComponent(batchId)}/${encodeURIComponent(jobId)}/log-lines`,
    { params },
  );
  return (data ?? []).map((row) => ({
    id: row.id,
    event_id: row.event_id ?? null,
    batch_id: batchId,
    job_id: row.job_id ?? jobId,
    timestamp: row.ts,
    level: row.level,
    message: row.line,
  }));
}

/**
 * Open an :class:`EventSource` to the job-scoped SSE log stream.
 *
 * ``token`` must be the JWT (read from ``localStorage``) — native
 * EventSource cannot set ``Authorization`` headers, so the backend
 * accepts the bearer via ``?token=`` for SSE only. Caller owns the
 * returned object: bind ``log_line`` / ``hello`` / ``displaced`` /
 * ``error`` listeners and ``close()`` it on unmount.
 */
export function jobLogStream(
  batchId: string,
  jobId: string,
  token: string,
): EventSource {
  const qs = `token=${encodeURIComponent(token)}`;
  const url =
    `/api/jobs/${encodeURIComponent(batchId)}/${encodeURIComponent(jobId)}` +
    `/logs/stream?${qs}`;
  return new EventSource(url);
}

// ---------------------------------------------------------------------------
// Host resource timeseries (stacked by batch_id, PR-B)
// ---------------------------------------------------------------------------

export interface HostTimeseriesBucket {
  ts: string;
  total: number | null;
  by_batch: Record<string, number>;
}

export interface HostTimeseriesOut {
  host: string;
  metric: string;
  buckets: HostTimeseriesBucket[];
  host_total_capacity: number | null;
}

export interface GetHostTimeseriesParams {
  metric?: 'gpu_mem_mb' | 'gpu_util_pct' | 'cpu_util_pct' | 'ram_mb';
  since?: string;        // ISO-8601 or relative like 'now-2h'
  bucket_seconds?: number;
}

export async function getHostTimeseries(
  host: string,
  params: GetHostTimeseriesParams = {},
): Promise<HostTimeseriesOut> {
  const { data } = await http.get<HostTimeseriesOut>(
    `/hosts/${encodeURIComponent(host)}/timeseries`,
    { params },
  );
  return data;
}

// ---------------------------------------------------------------------------
// OAuth feature-detect
// ---------------------------------------------------------------------------

export interface OAuthConfig {
  /** True iff GitHub OAuth is enabled AND fully configured on the server. */
  github: boolean;
}

/**
 * Fetch the OAuth feature-flag bundle so the Login page can conditionally
 * render the "Sign in with GitHub" button. Server returns 200 whether the
 * feature is on or off — the boolean inside the payload is what varies.
 */
export async function getOAuthConfig(): Promise<OAuthConfig> {
  const { data } = await http.get<OAuthConfig>('/auth/oauth/config');
  return data;
}

// ---------------------------------------------------------------------------
// Stop batch
// ---------------------------------------------------------------------------

/**
 * Request a cooperative stop for a running batch.
 *
 * Sets ``Batch.status = 'stopping'`` on the server and emits a
 * ``batch_stop_requested`` event so the reporter can poll and self-terminate.
 * No process is killed directly.
 *
 * Throws if the caller is not the batch owner / admin (403), or if the
 * batch does not exist (404).
 */
export async function stopBatch(batchId: string): Promise<void> {
  await http.post(`/batches/${encodeURIComponent(batchId)}/stop`);
}

// ---------------------------------------------------------------------------
// Soft delete (migration 021)
// ---------------------------------------------------------------------------

/**
 * Soft-delete a batch. Owner or admin only on the server side; the
 * frontend gates the trigger via ``usePermissions().canWrite``.
 */
export async function deleteBatch(batchId: string): Promise<void> {
  await http.delete(`/batches/${encodeURIComponent(batchId)}`);
}

/** Soft-delete a single job. Owner or admin only. */
export async function deleteJob(batchId: string, jobId: string): Promise<void> {
  await http.delete(
    `/jobs/${encodeURIComponent(batchId)}/${encodeURIComponent(jobId)}`,
  );
}

/** Soft-delete a project (cascades to its batches). Admin only. */
export async function deleteProject(project: string): Promise<void> {
  await http.delete(`/projects/${encodeURIComponent(project)}`);
}

/** Soft-delete a host. Admin only. */
export async function deleteHost(host: string): Promise<void> {
  await http.delete(`/hosts/${encodeURIComponent(host)}`);
}

export interface BulkDeleteSkip {
  id: string;
  reason: string;
}

export interface BulkDeleteResult {
  deleted: string[];
  skipped: BulkDeleteSkip[];
}

export async function bulkDeleteBatches(
  batchIds: string[],
): Promise<BulkDeleteResult> {
  const { data } = await http.post<BulkDeleteResult>('/batches/bulk-delete', {
    batch_ids: batchIds,
  });
  return data;
}

export async function bulkDeleteJobs(
  items: Array<{ batch_id: string; job_id: string }>,
): Promise<BulkDeleteResult> {
  const { data } = await http.post<BulkDeleteResult>('/jobs/bulk-delete', {
    items,
  });
  return data;
}

export async function bulkDeleteProjects(
  projects: string[],
): Promise<BulkDeleteResult> {
  const { data } = await http.post<BulkDeleteResult>(
    '/admin/projects/bulk-delete',
    { projects },
  );
  return data;
}

export async function bulkDeleteHosts(
  hosts: string[],
): Promise<BulkDeleteResult> {
  const { data } = await http.post<BulkDeleteResult>(
    '/admin/hosts/bulk-delete',
    { hosts },
  );
  return data;
}

// ---------------------------------------------------------------------------
// ETA helpers
// ---------------------------------------------------------------------------

export interface JobEtaInfo {
  job_id: string;
  elapsed_s: number;
  epochs_done: number;
  epochs_total: number;
  avg_epoch_time_s: number | null;
  eta_s: number | null;
  eta_iso: string | null;
}

/**
 * Per-job ETA from ``GET /api/jobs/{b}/{j}/eta``.
 */
export async function getJobEta(batchId: string, jobId: string): Promise<JobEtaInfo> {
  const { data } = await http.get<JobEtaInfo>(
    `/jobs/${encodeURIComponent(batchId)}/${encodeURIComponent(jobId)}/eta`,
  );
  return data;
}

/**
 * Bulk ETA for all jobs in a batch — single call, 10s server-side cache.
 * Returns a map of ``{ jobId → JobEtaInfo }``.
 */
export async function getBatchJobsEtaAll(batchId: string): Promise<Record<string, JobEtaInfo>> {
  const { data } = await http.get<Record<string, JobEtaInfo>>(
    `/batches/${encodeURIComponent(batchId)}/jobs/eta-all`,
  );
  return data;
}

export interface BatchEtaOut {
  batch_id: string;
  eta_seconds: number | null;
  pending_count: number;
  sampled_done_jobs: number;
}

/**
 * Batch-level ETA from ``GET /api/batches/{id}/eta`` (EMA over done jobs).
 */
export async function getBatchEta(batchId: string): Promise<BatchEtaOut> {
  const { data } = await http.get<BatchEtaOut>(
    `/batches/${encodeURIComponent(batchId)}/eta`,
  );
  return data;
}

// ---------------------------------------------------------------------------
// Observability — per-user GPU hours (#11)
// ---------------------------------------------------------------------------

export interface GpuHoursRow {
  user_id: number;
  username: string;
  gpu_hours: number;
  job_count: number;
}

/**
 * Per-user aggregated GPU hours over the last ``days`` days.
 *
 * Admins receive one row per user; non-admin callers receive a single
 * row for themselves (or zero rows when they haven't run anything).
 * Rows are sorted by gpu_hours descending server-side.
 */
export async function getGpuHoursByUser(
  days = 30,
): Promise<GpuHoursRow[]> {
  const { data } = await http.get<GpuHoursRow[]>('/stats/gpu-hours-by-user', {
    params: { days },
  });
  return data;
}

// ---------------------------------------------------------------------------
// Meta — empty-state hint catalog (#30)
// ---------------------------------------------------------------------------

export interface MetaHintsOut {
  /** Locale the backend resolved from Accept-Language (e.g. "en-US"). */
  locale: string;
  /**
   * Keys mirror the EmptyState variant prop — ``empty_hosts``,
   * ``empty_batches``, ``empty_jobs``, ``empty_projects``,
   * ``empty_notifications``, ``empty_pins``, ``empty_shared``,
   * ``empty_stars``, ``empty_search``, ``empty_events``,
   * ``empty_artifacts``. Both locales expose the same key set.
   */
  hints: Record<string, string>;
}

export async function getMetaHints(): Promise<MetaHintsOut> {
  const { data } = await http.get<MetaHintsOut>('/meta/hints');
  return data;
}

// ---------------------------------------------------------------------------
// Per-batch email subscription overrides
// ---------------------------------------------------------------------------

/**
 * One per-batch email subscription override row.
 *
 * Returned by ``GET /api/batches/{id}/email-subscription`` and ``PUT``;
 * absent (404) → caller falls back to project-level default.  The
 * ``event_kinds`` array is the authoritative list of event types the
 * owner wants emailed for THIS batch — anything not present is muted
 * regardless of the project default.
 */
export interface BatchEmailSubscription {
  batch_id: string;
  event_kinds: string[];
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface BatchEmailSubscriptionIn {
  event_kinds: string[];
  enabled: boolean;
}

/**
 * Fetch the caller's per-batch override for ``batch_id``.
 *
 * Returns ``null`` when no override exists (HTTP 404, the expected
 * empty-state for a freshly visited batch).  Other errors propagate
 * via the axios interceptor.  Only the batch owner is allowed to
 * read this — the backend returns 403 for everyone else, which the
 * caller can hide behind an ownership check.
 *
 * We use a bare ``axios`` call (not the shared ``http`` instance) so
 * the global "Not found" toast doesn't fire on the routine 404 — the
 * empty state is part of the contract, not an error.
 */
export async function getBatchEmailSubscription(
  batchId: string,
): Promise<BatchEmailSubscription | null> {
  const token = localStorage.getItem(LS_TOKEN_KEY);
  try {
    const { data } = await axios.get<BatchEmailSubscription>(
      `/api/batches/${encodeURIComponent(batchId)}/email-subscription`,
      {
        timeout: 10_000,
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      },
    );
    return data;
  } catch (err: unknown) {
    const status = (err as AxiosError)?.response?.status;
    if (status === 404) return null;
    throw err;
  }
}

/** Upsert the override row.  Returns the saved row. */
export async function setBatchEmailSubscription(
  batchId: string,
  body: BatchEmailSubscriptionIn,
): Promise<BatchEmailSubscription> {
  const { data } = await http.put<BatchEmailSubscription>(
    `/batches/${encodeURIComponent(batchId)}/email-subscription`,
    body,
  );
  return data;
}

/** Delete the override.  Idempotent — DELETE on missing row returns 204. */
export async function clearBatchEmailSubscription(
  batchId: string,
): Promise<void> {
  await http.delete(
    `/batches/${encodeURIComponent(batchId)}/email-subscription`,
  );
}
