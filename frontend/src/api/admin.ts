// Admin-only endpoints — wraps BACKEND-C /api/admin/* (requirements §7.6).
//
// Every call sits behind `Depends(require_admin)` on the backend; non-admin
// sessions get a 403 which the global interceptor already toasts. We don't
// retry or hide 403 here — the router guard should prevent the call from
// firing, and if it somehow does, the user deserves to see the warning.

import { http } from './client';
import type {
  AdminUser,
  AuditLogEntry,
  FeatureFlag,
  FeatureFlagValue,
  GenericSuccess,
  ListAuditParams,
} from '../types';

// -------- Users --------

export async function listUsers(): Promise<AdminUser[]> {
  const { data } = await http.get<AdminUser[]>('/admin/users');
  return data;
}

export async function banUser(id: number): Promise<GenericSuccess> {
  // Backend returns 403 when the caller tries to ban themselves; the UI
  // disables the button for the current user's row to prevent that, but the
  // interceptor will still toast if it slips through.
  const { data } = await http.post<GenericSuccess>(`/admin/users/${id}/ban`);
  return data;
}

export async function unbanUser(id: number): Promise<GenericSuccess> {
  // Also clears the login-failure lockout as a side effect (per BACKEND-C).
  const { data } = await http.post<GenericSuccess>(`/admin/users/${id}/unban`);
  return data;
}

// -------- Feature flags --------

export async function listFeatureFlags(): Promise<FeatureFlag[]> {
  const { data } = await http.get<FeatureFlag[]>('/admin/feature-flags');
  return data;
}

export async function updateFeatureFlag(
  key: string,
  value: FeatureFlagValue,
): Promise<FeatureFlag> {
  const { data } = await http.put<FeatureFlag>(
    `/admin/feature-flags/${encodeURIComponent(key)}`,
    { value },
  );
  return data;
}

// -------- Audit log --------

export async function listAuditLog(
  params: ListAuditParams = {},
): Promise<AuditLogEntry[]> {
  const { data } = await http.get<AuditLogEntry[]>('/admin/audit-log', { params });
  return data;
}

// -------- Backup status (#34) --------

/** One archived SQLite backup file on disk. */
export interface BackupFile {
  name: string;
  size_bytes: number;
  mtime: string;
}

/**
 * Response shape of ``GET /api/admin/backup-status``. Fields:
 *
 *  - ``enabled``: backup cron is on (``interval_h > 0``).
 *  - ``interval_h``: configured backup frequency, in hours.
 *  - ``keep_last_n``: retention — older archives are pruned.
 *  - ``last_backup_at``: ISO timestamp of the newest archive, or null.
 *  - ``backup_age_h``: hours since ``last_backup_at``. Null when no backup
 *    exists yet; shaded green / amber / red by the UI based on
 *    ``interval_h``.
 *  - ``recent_files``: up to ``keep_last_n`` newest archives, newest first.
 */
export interface BackupStatus {
  enabled: boolean;
  interval_h: number;
  keep_last_n: number;
  last_backup_at: string | null;
  backup_age_h: number | null;
  recent_files: BackupFile[];
}

export async function getBackupStatus(): Promise<BackupStatus> {
  const { data } = await http.get<BackupStatus>('/admin/backup-status');
  return data;
}

// -------- Public-demo projects --------

export interface PublicProjectMeta {
  project: string;
  is_public: boolean;
  public_description: string | null;
  published_at: string | null;
  published_by_user_id: number | null;
}

export async function publishProject(
  project: string,
  description?: string | null,
): Promise<PublicProjectMeta> {
  const { data } = await http.post<PublicProjectMeta>(
    `/admin/projects/${encodeURIComponent(project)}/publish`,
    { description: description ?? null },
  );
  return data;
}

export async function unpublishProject(project: string): Promise<void> {
  await http.post(`/admin/projects/${encodeURIComponent(project)}/unpublish`);
}

export async function listAdminPublicProjects(): Promise<PublicProjectMeta[]> {
  const { data } = await http.get<PublicProjectMeta[]>('/admin/projects/public');
  return data;
}

// -------- System config (DB-driven runtime config) --------

/** A single ``system_config`` entry as returned by the admin API. */
export interface SystemConfigItem {
  key: string;
  value: unknown;
  encrypted: boolean;
  /** 'db' (overridden), 'env' (env var), or 'default' (built-in). */
  source: 'db' | 'env' | 'default';
  description?: string | null;
  updated_at?: string | null;
  updated_by?: number | null;
}

export type SystemConfigGroup = 'oauth' | 'smtp' | 'retention' | 'feature_flags' | 'demo';

export type SystemConfigBundle = Record<SystemConfigGroup, SystemConfigItem[]>;

/** Fetch every group keyed by name. */
export async function listSystemConfig(): Promise<SystemConfigBundle> {
  const { data } = await http.get<SystemConfigBundle>('/admin/system-config');
  return data;
}

/** Fetch a single group as a flat list. */
export async function getSystemConfigGroup(
  group: SystemConfigGroup,
): Promise<SystemConfigItem[]> {
  const { data } = await http.get<SystemConfigItem[]>(
    `/admin/system-config/${encodeURIComponent(group)}`,
  );
  return data;
}

/** Upsert one entry. */
export async function putSystemConfig(
  group: SystemConfigGroup,
  key: string,
  value: unknown,
  options: { encrypted?: boolean; description?: string } = {},
): Promise<SystemConfigItem> {
  const body: Record<string, unknown> = { value };
  if (options.encrypted !== undefined) body.encrypted = options.encrypted;
  if (options.description !== undefined) body.description = options.description;
  const { data } = await http.put<SystemConfigItem>(
    `/admin/system-config/${encodeURIComponent(group)}/${encodeURIComponent(key)}`,
    body,
  );
  return data;
}

/** Remove the override so reads fall back to env / default. */
export async function deleteSystemConfig(
  group: SystemConfigGroup,
  key: string,
): Promise<void> {
  await http.delete(
    `/admin/system-config/${encodeURIComponent(group)}/${encodeURIComponent(key)}`,
  );
}

// -------- Security: JWT secret rotation (v0.2 #109) --------

/** Read-only state for the Settings → Admin → Security panel. */
export interface JwtRotationStatus {
  rotated_at: string | null;
  has_previous: boolean;
  previous_expires_at: string | null;
  grace_seconds: number;
}

/** Result of a successful ``POST /admin/security/jwt/rotate`` call. */
export interface JwtRotationResult {
  rotated_at: string;
  grace_seconds: number;
}

/** Fetch the current rotation state. Never returns secret material. */
export async function getJwtRotationStatus(): Promise<JwtRotationStatus> {
  const { data } = await http.get<JwtRotationStatus>('/admin/security/jwt/status');
  return data;
}

/** Rotate the JWT signing secret. Existing tokens stay valid for the grace window. */
export async function rotateJwtSecret(): Promise<JwtRotationResult> {
  const { data } = await http.post<JwtRotationResult>('/admin/security/jwt/rotate');
  return data;
}
