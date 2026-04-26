/**
 * /api/admin/email/* and /api/me/subscriptions — email + notifications.
 *
 * Types here mirror BE-1's pydantic shapes in
 * ``backend/backend/schemas/email.py``. The backend uses ``smtp_`` prefixed
 * field names (``smtp_host``, ``smtp_port``, …); we keep the same names
 * in the client so no renaming layer is needed.
 */

import { http } from './client';

// ---------------------------------------------------------------------------
// SMTP configuration
// ---------------------------------------------------------------------------

/**
 * Shape of ``GET /api/admin/email/smtp``.
 *
 * Per BE-1, ``smtp_password`` is NEVER returned with the real value — the
 * server substitutes the literal sentinel ``"***"`` on every read. On the
 * PUT path, echoing ``"***"`` back tells the backend to preserve whatever
 * is already stored.
 */
export interface SmtpConfig {
  enabled: boolean;
  smtp_host: string | null;
  smtp_port: number;
  smtp_username: string | null;
  smtp_password: string;
  smtp_from_address: string | null;
  smtp_from_name: string | null;
  use_tls: boolean;
  use_ssl: boolean;
  updated_at?: string | null;
  updated_by_user_id?: number | null;
}

/** Body for ``PUT /api/admin/email/smtp``. */
export type SmtpConfigIn = Omit<SmtpConfig, 'updated_at' | 'updated_by_user_id'>;

export interface SmtpTestResult {
  ok: boolean;
  error?: string | null;
}

export async function getSmtpConfig(): Promise<SmtpConfig> {
  const { data } = await http.get<SmtpConfig>('/admin/email/smtp');
  return data;
}

export async function updateSmtpConfig(payload: SmtpConfigIn): Promise<void> {
  await http.put('/admin/email/smtp', payload);
}

export async function testSmtpConfig(payload: SmtpConfigIn): Promise<SmtpTestResult> {
  const { data } = await http.post<SmtpTestResult>('/admin/email/smtp/test', payload);
  return data;
}

// ---------------------------------------------------------------------------
// Email templates
// ---------------------------------------------------------------------------

export interface EmailTemplate {
  id: number;
  event_type: string;
  locale: string;
  subject: string;
  body_html: string;
  body_text: string;
  is_system: boolean;
  updated_at?: string | null;
  updated_by_user_id?: number | null;
  /**
   * Optional per-event variable catalogue. BE-1's initial cut doesn't
   * surface this yet; the page falls back to "no variables declared"
   * when the array is absent or empty.
   */
  available_variables?: string[];
}

export interface EmailTemplatePreview {
  subject: string;
  body_html: string;
  body_text: string;
}

export interface EmailTemplateUpdateIn {
  subject: string;
  body_html: string;
  body_text: string;
}

export async function listEmailTemplates(): Promise<EmailTemplate[]> {
  const { data } = await http.get<EmailTemplate[]>('/admin/email/templates');
  return data;
}

export async function getEmailTemplate(id: number): Promise<EmailTemplate> {
  const { data } = await http.get<EmailTemplate>(`/admin/email/templates/${id}`);
  return data;
}

export async function updateEmailTemplate(
  id: number,
  payload: EmailTemplateUpdateIn,
): Promise<void> {
  await http.put(`/admin/email/templates/${id}`, payload);
}

/**
 * Render the template against a sample context. Passing `payload` lets the
 * caller preview unsaved edits without persisting them; omit to render the
 * stored row.
 */
export async function previewEmailTemplate(
  id: number,
  payload?: EmailTemplateUpdateIn,
): Promise<EmailTemplatePreview> {
  const { data } = await http.post<EmailTemplatePreview>(
    `/admin/email/templates/${id}/preview`,
    payload ?? {},
  );
  return data;
}

export async function resetEmailTemplate(id: number): Promise<void> {
  await http.post(`/admin/email/templates/${id}/reset`);
}

// ---------------------------------------------------------------------------
// Per-user subscriptions
// ---------------------------------------------------------------------------

/**
 * A single subscription row. ``project`` is null for the global default
 * (the per-event fallback applied when no project-specific row exists).
 */
export interface SubscriptionRow {
  event_type: string;
  project: string | null;
  enabled: boolean;
}

/** Body for ``PATCH /api/me/subscriptions``. */
export interface SubscriptionBulkIn {
  subscriptions: SubscriptionRow[];
}

export async function getMySubscriptions(): Promise<SubscriptionRow[]> {
  const { data } = await http.get<SubscriptionRow[]>('/me/subscriptions');
  return data;
}

export async function patchSubscriptions(
  subscriptions: SubscriptionRow[],
): Promise<void> {
  await http.patch('/me/subscriptions', { subscriptions });
}

// ---------------------------------------------------------------------------
// Anonymous unsubscribe
// ---------------------------------------------------------------------------

export interface UnsubscribeResult {
  ok: boolean;
  /**
   * Human-readable outcome. BE-1 uses this for both "unsubscribed from
   * <event>" and "token expired" messaging, so the UI surfaces it as-is.
   */
  detail?: string | null;
}

/**
 * The `?token=...` in the unsubscribe link is a signed one-shot string.
 * The endpoint itself is anonymous-accessible on the backend; we deliberately
 * call it through axios so the request picks up base URL + error handling
 * like every other page. If the browser still has a stored JWT (user happens
 * to be logged in), it is attached but ignored by the backend.
 */
export async function unsubscribeWithToken(token: string): Promise<UnsubscribeResult> {
  const { data } = await http.post<UnsubscribeResult>('/unsubscribe', null, {
    params: { token },
  });
  return data;
}

// ---------------------------------------------------------------------------
// Per-project multi-recipient list (v0.1.4)
// ---------------------------------------------------------------------------

/**
 * One :class:`ProjectNotificationRecipient` row as returned by the backend.
 *
 * The unsubscribe token is intentionally NOT exposed by the API — it lives
 * only in outgoing email footers so a one-click public link is the
 * recipient's exit path. The frontend never touches it.
 */
export interface ProjectRecipient {
  id: number;
  project: string;
  email: string;
  event_kinds: string[];
  enabled: boolean;
  added_by_user_id: number;
  created_at?: string | null;
  updated_at?: string | null;
}

/** Body for ``POST /api/projects/{project}/recipients``. */
export interface ProjectRecipientIn {
  email: string;
  event_kinds: string[];
  enabled: boolean;
}

/** Body for ``PATCH /api/projects/{project}/recipients/{id}``. Every field optional. */
export interface ProjectRecipientPatch {
  email?: string;
  event_kinds?: string[];
  enabled?: boolean;
}

export async function listProjectRecipients(
  project: string,
): Promise<ProjectRecipient[]> {
  const { data } = await http.get<ProjectRecipient[]>(
    `/projects/${encodeURIComponent(project)}/recipients`,
  );
  return data;
}

export async function addProjectRecipient(
  project: string,
  payload: ProjectRecipientIn,
): Promise<ProjectRecipient> {
  const { data } = await http.post<ProjectRecipient>(
    `/projects/${encodeURIComponent(project)}/recipients`,
    payload,
  );
  return data;
}

export async function updateProjectRecipient(
  project: string,
  id: number,
  patch: ProjectRecipientPatch,
): Promise<ProjectRecipient> {
  const { data } = await http.patch<ProjectRecipient>(
    `/projects/${encodeURIComponent(project)}/recipients/${id}`,
    patch,
  );
  return data;
}

export async function deleteProjectRecipient(
  project: string,
  id: number,
): Promise<void> {
  await http.delete(
    `/projects/${encodeURIComponent(project)}/recipients/${id}`,
  );
}
