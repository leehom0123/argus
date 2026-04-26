// /api/me/* user-self endpoints (#108).
//
// Sits next to the auth wrappers because the matching pages
// (Settings → Profile + Settings → Notifications) read both the auth
// store and these self-care knobs in the same render. Keeping them in a
// dedicated module avoids further bloat on auth.ts which is already
// busy with login / OAuth / session plumbing.

import { http } from './client';

/**
 * Per-user notification preference defaults. These propagate to NEW
 * batches when the owner runs them; existing per-batch
 * ``batch_email_subscription`` rows always shadow these values at
 * dispatch time, so flipping a key here doesn't retroactively silence
 * a running batch.
 */
export interface NotificationPrefs {
  notify_batch_done: boolean;
  notify_batch_failed: boolean;
  notify_job_failed: boolean;
  notify_diverged: boolean;
  notify_job_idle: boolean;
}

/** GET /api/me/notification_prefs — returns defaults if never customised. */
export async function getNotificationPrefs(): Promise<NotificationPrefs> {
  const { data } = await http.get<NotificationPrefs>(
    '/me/notification_prefs',
  );
  return data;
}

/**
 * PUT /api/me/notification_prefs — total-update semantics. The body
 * fully replaces the stored row; pydantic enforces every key being
 * present so partial-write ambiguity is impossible.
 */
export async function putNotificationPrefs(
  body: NotificationPrefs,
): Promise<NotificationPrefs> {
  const { data } = await http.put<NotificationPrefs>(
    '/me/notification_prefs',
    body,
  );
  return data;
}

/**
 * POST /api/me/resend_verification — re-mails the verify-email link.
 *
 * Backend semantics:
 *   * 200 ``{ok:true}``  — token minted + email queued.
 *   * 409                — caller's email is already verified.
 *   * 429                — 1/min/user bucket is empty; ``Retry-After``
 *                          header carries the wait in seconds.
 *
 * Callers handle 409 by swapping the banner for an "already verified"
 * toast (the auth store should also ``fetchMe()`` to pick up the flag
 * change, in case verification happened in another tab).
 */
export async function resendVerification(): Promise<{ ok: boolean }> {
  const { data } = await http.post<{ ok: boolean }>(
    '/me/resend_verification',
  );
  return data;
}
