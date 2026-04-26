// ------------------------------------------------------------------
// Shared API response types. Kept in sync with schemas/event_v1.json.
// ------------------------------------------------------------------

export type BatchStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'failed'
  | 'partial'
  | 'stopping'
  | 'stopped'
  | 'requested'
  | 'stalled'
  | 'divergent';
export type JobStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped';

/** Reproducibility snapshot attached to a batch (migration 014). */
export interface EnvSnapshot {
  git_sha?: string | null;
  git_branch?: string | null;
  git_dirty?: boolean | null;
  python_version?: string | null;
  pip_freeze?: string[] | null;
  hydra_config_digest?: string | null;
  hydra_config_content?: string | null;
  hostname?: string | null;
}

export interface Batch {
  id: string;
  project: string;
  user?: string | null;
  experiment_type?: string | null;
  status: BatchStatus;
  n_total: number;
  n_done: number;
  n_failed: number;
  start_time?: string | null;
  end_time?: string | null;
  host?: string | null;
  command?: string | null;
  /** Set on batches created via "Rerun with overrides"; null otherwise. */
  source_batch_id?: string | null;
  /** Reproducibility snapshot (git SHA, pip freeze, Hydra config). Null for old batches. */
  env_snapshot?: EnvSnapshot | null;
  // extra permissive bag so forward-compat fields from backend don't break us
  extra?: Record<string, unknown>;
}

export interface JobMetrics {
  // Forecast metrics are the common ones, but backend may attach anything.
  MSE?: number;
  MAE?: number;
  R2?: number;
  PCC?: number;
  SCC?: number;
  RMSE?: number;
  [key: string]: number | undefined;
}

export interface Job {
  id: string;
  batch_id: string;
  model?: string | null;
  dataset?: string | null;
  status: JobStatus;
  start_time?: string | null;
  end_time?: string | null;
  elapsed_s?: number | null;
  metrics?: JobMetrics | null;
  run_dir?: string | null;
  /**
   * Guardrails (#13): true when the idle-job watchdog has flagged this job
   * for sustained GPU util < 5%. Advisory — the job is NOT killed. Backend
   * emits a ``job_idle_flagged`` event in the same transaction.
   */
  is_idle_flagged?: boolean;
  extra?: Record<string, unknown>;
}

export interface EpochPoint {
  epoch: number;
  train_loss?: number | null;
  val_loss?: number | null;
  lr?: number | null;
  [key: string]: number | null | undefined;
}

export interface ResourceSnapshot {
  timestamp: string;
  host?: string;
  gpu_util_pct?: number | null;
  gpu_mem_mb?: number | null;
  gpu_mem_total_mb?: number | null;
  gpu_temp_c?: number | null;
  cpu_util_pct?: number | null;
  ram_mb?: number | null;
  ram_total_mb?: number | null;
  disk_free_mb?: number | null;
  /**
   * Total disk capacity (MB) on the partition the run dir lives on. Optional;
   * older reporters and pre-migration-020 snapshot rows leave this null. When
   * present, frontend bars compute used% = (total - free) / total instead of
   * the legacy free-GB pressure heuristic.
   */
  disk_total_mb?: number | null;
  /** Process ID of the reporter; extracted from snapshot.extra JSON when present. */
  pid?: number | null;
  [key: string]: unknown;
}

export type BatchScope = 'mine' | 'shared' | 'all';

export interface ListBatchesParams {
  user?: string;
  project?: string;
  status?: BatchStatus;
  since?: string;
  limit?: number;
  scope?: BatchScope;
}

export interface ListResourcesParams {
  host?: string;
  since?: string;
  limit?: number;
}

/** Filter shape for ``GET /api/jobs`` (the global jobs list). */
export interface ListJobsParams {
  status?: JobStatus | string;
  project?: string;
  host?: string;
  batch_id?: string;
  /**
   * ISO 8601 timestamp or relative shorthand (``24h`` / ``30m`` / ``7d``).
   * Backend accepts either form and resolves to ``Job.start_time >= …``.
   */
  since?: string;
  page?: number;
  page_size?: number;
}

/** One row of the global jobs list — Job + batch context (project/host/name). */
export interface GlobalJobItem {
  job: Job;
  project: string;
  host?: string | null;
  batch_name?: string | null;
}

/** Paginated wrapper for ``GET /api/jobs``. */
export interface GlobalJobListOut {
  items: GlobalJobItem[];
  total: number;
  page: number;
  page_size: number;
}

// ------------------------------------------------------------------
// Auth types — mirror backend /api/auth/* contract (BACKEND-A).
// ------------------------------------------------------------------

export interface User {
  id: number;
  username: string;
  email: string;
  is_admin: boolean;
  email_verified: boolean;
  created_at?: string | null;
  last_login?: string | null;
  /** GitHub login (e.g. "octocat") when the account is linked; null otherwise. */
  github_login?: string | null;
  /**
   * True when the user has a local password. False for GitHub-provisioned
   * users who haven't run the "set a password" flow — backend blocks unlink
   * in that state.
   */
  has_password?: boolean;
  /**
   * Legacy per-user flag. Kept on the type so we don't choke on backend
   * responses that still include it, but no longer read by the UI — demo
   * visibility is resolved entirely server-side (signed-in users never
   * see demo entries; anonymous visitors reach them via /demo/*).
   */
  hide_demo?: boolean;
}

export interface AuthTokenResponse {
  access_token: string;
  token_type: string; // usually "bearer"
  expires_in: number; // seconds
  user: User;
}

export interface RefreshTokenResponse {
  access_token: string;
  expires_in: number;
}

export interface RegisterIn {
  username: string;
  email: string;
  password: string;
}

export interface RegisterOut {
  user_id: number;
  require_verify: boolean;
}

export interface LoginIn {
  username_or_email: string;
  password: string;
}

export interface VerifyEmailIn {
  token: string;
}

export interface RequestPasswordResetIn {
  email: string;
}

export interface ResetPasswordIn {
  token: string;
  new_password: string;
}

export interface GenericSuccess {
  success: boolean;
}

/**
 * Error thrown when backend returns 423 Locked on login (too many attempts).
 * UI shows countdown until `retry_after` seconds elapse.
 */
export interface LockedError {
  kind: 'locked';
  retry_after: number; // seconds
  message: string;
}

// ------------------------------------------------------------------
// Token / Share / Public share types — mirror BACKEND-B / BACKEND-C contract.
// ------------------------------------------------------------------

export type TokenScope = 'reporter' | 'viewer';
export type SharePermission = 'viewer' | 'editor';

/** Token record as returned by GET /api/tokens (no plaintext). */
export interface ApiToken {
  id: number;
  name: string;
  /** e.g. "em_live_" */
  prefix: string;
  /** first few chars of the plaintext token, e.g. "abcd…" — for display only */
  display_hint?: string | null;
  scope: TokenScope;
  created_at: string;
  last_used?: string | null;
  expires_at?: string | null;
  revoked?: boolean;
}

/** POST /api/tokens response — the plaintext `token` appears here ONCE. */
export interface TokenCreateResponse extends ApiToken {
  /** Full plaintext token "em_live_XXXXXXXXXXXXXXXXXXXX". Shown to the user once. */
  token: string;
}

export interface TokenCreateRequest {
  name: string;
  scope: TokenScope;
  /** ISO datetime; omit for "never expires" */
  expires_at?: string | null;
}

/** Share of a single batch with a named grantee. */
export interface BatchShare {
  batch_id: string;
  grantee_id: number;
  grantee_username: string;
  permission: SharePermission;
  created_at?: string | null;
}

/** Share of an entire project with a named grantee. */
export interface ProjectShare {
  project: string;
  grantee_id: number;
  grantee_username: string;
  permission: SharePermission;
  created_at?: string | null;
}

/** Anonymous public-link record. */
export interface PublicShare {
  slug: string;
  batch_id: string;
  url: string;
  created_at?: string | null;
  expires_at?: string | null;
  view_count?: number;
}

// ------------------------------------------------------------------
// Active sessions — Settings > Sessions panel (issue #31).
// Mirrors backend.schemas.auth.ActiveSessionOut.
// ------------------------------------------------------------------

/** One row in GET /api/auth/sessions — a currently-valid JWT for the caller. */
export interface ActiveSession {
  /** JWT ID. Stable per-token, used as row key + revoke path segment. */
  jti: string;
  issued_at: string;
  expires_at: string;
  user_agent?: string | null;
  ip?: string | null;
  last_seen_at?: string | null;
  /** True for the JWT powering this very request — UI disables revoke on it. */
  is_current: boolean;
}

// ------------------------------------------------------------------
// Admin types — mirror BACKEND-C /api/admin/* contract (requirements §7.6).
// ------------------------------------------------------------------

/**
 * User row as returned by GET /api/admin/users. Superset of `User` — adds
 * `is_active` (false when banned) and timestamps that admins use for triage.
 */
export interface AdminUser {
  id: number;
  username: string;
  email: string;
  is_admin: boolean;
  is_active: boolean;
  email_verified: boolean;
  created_at?: string | null;
  last_login?: string | null;
}

/**
 * A global feature flag. `value` is the parsed JSON payload — for MVP this is
 * bool, number, or string; we keep it permissive so future flags (arrays,
 * nested objects) don't require a type change.
 */
export type FeatureFlagValue = boolean | number | string | null;

export interface FeatureFlag {
  key: string;
  value: FeatureFlagValue;
  updated_at?: string | null;
}

/** Audit-log row from GET /api/admin/audit-log. */
export interface AuditLogEntry {
  id: number;
  user_id?: number | null;
  username?: string | null;
  action: string; // 'token_create' | 'share_add' | 'user_ban' | ...
  target_type?: string | null;
  target_id?: string | null;
  /** JSON blob — object or scalar; rendered as preview in UI. */
  metadata?: unknown;
  timestamp: string;
  ip_address?: string | null;
}

export interface ListAuditParams {
  since?: string;
  action?: string;
  limit?: number;
  offset?: number;
}

// ------------------------------------------------------------------
// Dashboard IA types (requirements §16-17).
//
// Response shapes are based on requirements examples and design notes.
// Backend (dashboard-backend-eng-r2) is implementing these in parallel, so we
// keep fields optional/permissive for MVP and tighten when the wire contract
// lands in QA.
// ------------------------------------------------------------------

/** Top-of-dashboard metric tile counts. */
export interface DashboardCounters {
  running_batches?: number;
  jobs_running?: number;
  jobs_done_24h?: number;
  jobs_failed_24h?: number;
  active_hosts?: number;
  /** Raw 0–1 float from the API (multiply by 100 for display). */
  avg_gpu_util?: number | null;
  [key: string]: number | null | undefined;
}

/** One row per project on the dashboard + project list. */
/** One entry in the top_models density row on a project card. */
export interface ProjectTopModel {
  model: string | null;
  dataset: string | null;
  metric_name: string;
  metric_value: number;
}

export interface ProjectSummary {
  project: string;
  running_batches?: number;
  total_batches?: number;
  /** Backend-canonical batch count — same value, different key. */
  n_batches?: number;
  jobs_done?: number;
  jobs_failed?: number;
  jobs_running?: number;
  /** Best metric across the project; key depends on `best_metric_key`. */
  best_metric?: number | null;
  best_metric_key?: string | null;
  /** ISO timestamp of the last event seen for any batch in the project. */
  last_event_at?: string | null;
  /** ETA in seconds to finish all running batches in this project. */
  eta?: number | null;
  /** Alternate ETA key emitted by /api/dashboard project cards. */
  eta_seconds?: number | null;
  is_starred?: boolean;
  /** True for the built-in demo project (badge target in the UI). */
  is_demo?: boolean;
  owners?: string[];
  collaborators?: string[];
  // ── v0.1.3 density extension ─────────────────────────────────────────
  /** failed / (done + failed); null when nothing has ended yet. */
  failure_rate?: number | null;
  /** Cumulative GPU hours across every job in the project. */
  gpu_hours?: number | null;
  /** Top-3 (model × dataset) winners by the project's headline metric. */
  top_models?: ProjectTopModel[];
  /** Per-day batch start counts for the last 7 days (oldest → newest). */
  batch_volume_7d?: number[];
  [key: string]: unknown;
}

/** One activity-feed entry. */
export interface ActivityItem {
  id?: string | number;
  timestamp: string;
  event_type: string; // batch_start / batch_done / job_failed / ...
  batch_id?: string | null;
  job_id?: string | null;
  project?: string | null;
  user?: string | null;
  message?: string | null;
  level?: 'info' | 'warn' | 'error' | null;
}

/** One running job listed on a host card (v0.1.3 density row). */
export interface HostRunningJob {
  job_id: string;
  model: string | null;
  dataset: string | null;
  user: string | null;
  pid: number | null;
}

/** One host summary card on the dashboard right-rail. */
export interface HostSummary {
  host: string;
  gpu_util_pct?: number | null;
  gpu_mem_mb?: number | null;
  gpu_mem_total_mb?: number | null;
  gpu_temp_c?: number | null;
  cpu_util_pct?: number | null;
  ram_mb?: number | null;
  ram_total_mb?: number | null;
  disk_free_mb?: number | null;
  /** Total disk MB; populated when the reporter emits it (migration 020). */
  disk_total_mb?: number | null;
  running_jobs?: number;
  /** Up to 5 currently-running jobs on this host (v0.1.3). */
  running_jobs_top5?: HostRunningJob[];
  last_seen?: string | null;
  warnings?: string[];
}

/** Notification panel item. */
export interface DashNotification {
  id?: string | number;
  timestamp: string;
  level: 'info' | 'warn' | 'error';
  title: string;
  body?: string | null;
  link?: string | null;
  read?: boolean;
}

/** Response payload for GET /api/dashboard. */
export interface DashboardData {
  counters?: DashboardCounters;
  projects?: ProjectSummary[];
  activity?: ActivityItem[];
  hosts?: HostSummary[];
  notifications?: DashNotification[];
  /** Bag for forward-compat fields. */
  [key: string]: unknown;
}

/** A running job slot on an active batch card. */
export interface RunningJobSlot {
  job_id: string;
  model?: string | null;
  dataset?: string | null;
  epoch?: number | null;
  total_epochs?: number | null;
  val_loss?: number | null;
  train_loss?: number | null;
  /** Last few val_loss values for a mini sparkline. */
  loss_trace?: number[] | null;
  trend?: 'down' | 'up' | 'flat' | null;
}

/** Active batch card payload — one per running batch. */
export interface ActiveBatchCard {
  batch_id: string;
  project?: string | null;
  user?: string | null;
  host?: string | null;
  status?: BatchStatus;
  n_total?: number;
  n_done?: number;
  n_failed?: number;
  n_running?: number;
  running_jobs?: RunningJobSlot[];
  elapsed_s?: number | null;
  eta_s?: number | null;
  gpu_util_pct?: number | null;
  gpu_mem_mb?: number | null;
  gpu_mem_total_mb?: number | null;
  disk_free_mb?: number | null;
  is_stalled?: boolean;
  last_event_age_s?: number | null;
  warnings?: string[];
  /** Compact trend for a sparkline — e.g. recent val_loss or progress-over-time. */
  sparkline?: number[] | null;
  best_so_far_pct?: number | null;
  start_time?: string | null;
  [key: string]: unknown;
}

/** One row in a project-level leaderboard. */
export interface LeaderboardRow {
  batch_id?: string | null;
  model?: string | null;
  dataset?: string | null;
  /** Primary sort metric value (min over the `metric_name` column). */
  best_metric?: number | null;
  /** Which metric was used for ranking (null when job has no metrics). */
  metric_name?: string | null;
  job_id?: string | null;
  status?: JobStatus | null;
  /** Extracted from metrics dict (train_epochs / epochs key). */
  train_epochs?: number | null;
  elapsed_s?: number | null;
  /** Full metrics map from the winning job (all numeric keys the reporter sent). */
  metrics?: JobMetrics | null;
  [key: string]: unknown;
}

/** Cell in a (model × dataset) matrix. */
export interface MatrixCell {
  model: string;
  dataset: string;
  value: number | null;
  batch_id?: string | null;
  status?: JobStatus | null;
}

/** Shape returned by GET /api/projects/:project/matrix. */
export interface MatrixData {
  metric: string;
  models: string[];
  datasets: string[];
  cells: MatrixCell[];
  /**
   * Parallel to ``cells``: batch IDs that produced each cell's value,
   * newest-first (up to 3). ``null`` when no value exists for that cell.
   * Populated by the backend when the matrix endpoint returns ``batch_ids``.
   */
  batchIds?: (string[] | null)[];
}

/** GPU-hours time series + per-slot heatmap for the Resources tab. */
export interface ResourceTimeseriesPoint {
  timestamp: string;
  gpu_hours?: number | null;
  jobs_running?: number | null;
  [key: string]: unknown;
}

export interface ProjectResourcesData {
  timeseries?: ResourceTimeseriesPoint[];
  /** 7×24 grid keyed by weekday/hour for a calendar heatmap. */
  hourly_heatmap?: number[][] | null;
  by_host?: Array<{ host: string; gpu_hours: number }> | null;
  total_gpu_hours?: number | null;
}

/** Full aggregated payload for GET /api/projects/:project. */
export interface ProjectDetail {
  project: string;
  owner?: string | null;
  /** Project owners list (live API field). */
  owners?: string[];
  /** Legacy alias kept for backward compat. */
  collaborators?: string[];
  created_at?: string | null;
  starred?: boolean;
  is_starred?: boolean;
  // Summary strip — live API uses n_batches (not total_batches)
  n_batches?: number;
  /** @deprecated live API emits n_batches */
  total_batches?: number;
  batches_this_week?: number;
  running_batches?: number;
  failure_rate?: number | null;
  gpu_hours?: number | null;
  /** Live API: {name, value}. Legacy: plain number. */
  best_metric?: { name: string; value: number } | number | null;
  /** @deprecated use best_metric.name */
  best_metric_key?: string | null;
  // Shortcuts for the recent card list; fuller data comes from dedicated
  // /active-batches & /leaderboard endpoints when the tab is first visited.
  active_batches?: ActiveBatchCard[];
  recent_batches?: Batch[];
  [key: string]: unknown;
}

/** Star record (project or batch). */
export interface Star {
  target_type: 'project' | 'batch';
  target_id: string;
  starred_at?: string | null;
}

/** Pin record (batch only). */
export interface Pin {
  batch_id: string;
  pinned_at?: string | null;
}

/** Per-batch entry in the compare payload. */
export interface CompareBatch {
  batch_id: string;
  project?: string | null;
  model?: string | null;
  dataset?: string | null;
  status?: BatchStatus | null;
  metrics?: JobMetrics | null;
  /** Best (lowest-val-loss) job's loss curve for the comparison plot. */
  loss_curve?: EpochPoint[] | null;
  /** Optional per-batch matrix so we can diff them client-side. */
  matrix?: MatrixData | null;
  [key: string]: unknown;
}

/** Response shape for GET /api/compare?batches=a,b,c. */
export interface CompareData {
  batches: CompareBatch[];
  /** Common metric keys across all compared batches. */
  metric_keys?: string[];
}

/** Health info for the BatchCard "stalled" badge + warnings. */
export interface BatchHealth {
  is_stalled?: boolean;
  last_event_age_s?: number | null;
  failure_count?: number | null;
  warnings?: string[] | null;
  running_jobs?: number | null;
}

/** ETA prediction (EMA-based). */
export interface BatchETA {
  eta_seconds?: number | null;
  jobs_remaining?: number | null;
  confidence?: number | null;
}
