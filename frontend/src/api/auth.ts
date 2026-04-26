// Auth API wrapper — one function per endpoint in the BACKEND-A contract.
// All calls route through the shared `http` axios instance so they pick up
// the JWT header and interceptors (401/423/etc.) automatically.

import { http } from './client';
import type {
  ActiveSession,
  AuthTokenResponse,
  GenericSuccess,
  LoginIn,
  RefreshTokenResponse,
  RegisterIn,
  RegisterOut,
  RequestPasswordResetIn,
  ResetPasswordIn,
  User,
  VerifyEmailIn,
} from '../types';

export async function register(body: RegisterIn): Promise<RegisterOut> {
  const { data } = await http.post<RegisterOut>('/auth/register', body);
  return data;
}

export async function login(body: LoginIn): Promise<AuthTokenResponse> {
  const { data } = await http.post<AuthTokenResponse>('/auth/login', body);
  return data;
}

export async function verifyEmail(body: VerifyEmailIn): Promise<GenericSuccess> {
  const { data } = await http.post<GenericSuccess>('/auth/verify-email', body);
  return data;
}

export async function requestPasswordReset(
  body: RequestPasswordResetIn,
): Promise<GenericSuccess> {
  const { data } = await http.post<GenericSuccess>('/auth/request-password-reset', body);
  return data;
}

export async function resetPassword(body: ResetPasswordIn): Promise<GenericSuccess> {
  const { data } = await http.post<GenericSuccess>('/auth/reset-password', body);
  return data;
}

/**
 * Change the caller's password from within an authenticated session.
 * Backend returns 200 {ok:true} on success, 401 on wrong current password,
 * 400 on new==current, 422 on too-short new password, 429 when rate-limited.
 *
 * All sibling active_sessions rows for the caller's user get revoked on
 * success; the current JWT stays valid so the browser tab doesn't sign out.
 */
export async function changePassword(
  current: string,
  next: string,
): Promise<GenericSuccess> {
  const { data } = await http.post<GenericSuccess>('/auth/change-password', {
    current_password: current,
    new_password: next,
  });
  return data;
}

/**
 * Begin the change-email flow. Backend returns 200 {ok:true} on success
 * (a verification email has been mailed to ``new_email``); 401 on wrong
 * current password; 400 when the new email is the same as the current
 * one or already taken; 429 when rate-limited (3/hour).
 *
 * The user's actual email row is **not** updated until they click the
 * confirmation link mailed to the new address.
 */
export async function changeEmail(
  new_email: string,
  current_password: string,
): Promise<GenericSuccess> {
  const { data } = await http.post<GenericSuccess>('/auth/change-email', {
    new_email,
    current_password,
  });
  return data;
}

/**
 * Public confirm endpoint for the change-email flow. The token itself
 * is the credential (mailed to the new address). 200 on success, 410 on
 * replay/expired, 400 on a bad/unknown token.
 */
export async function verifyNewEmail(token: string): Promise<GenericSuccess> {
  const { data } = await http.get<GenericSuccess>('/auth/verify-new-email', {
    params: { token },
  });
  return data;
}

export async function logout(): Promise<GenericSuccess> {
  // Backend is idempotent; returns 200 even if token is already expired.
  const { data } = await http.post<GenericSuccess>('/auth/logout');
  return data;
}

export async function refresh(): Promise<RefreshTokenResponse> {
  const { data } = await http.post<RefreshTokenResponse>('/auth/refresh');
  return data;
}

export async function getMe(): Promise<User> {
  const { data } = await http.get<User>('/auth/me');
  return data;
}

// ---------------------------------------------------------------------------
// GitHub account bind / unbind (settings page).
//
// Bind mirrors the login-side OAuth dance: we navigate the browser to
// /api/auth/oauth/github/link/start; the backend 302-redirects to GitHub
// and eventually lands on /login/oauth/complete?bind_ok=1 (or ?bind_error=…).
// Since that's a hard navigation we expose a URL helper rather than an
// axios call — the caller does ``window.location.href = url``.
// ---------------------------------------------------------------------------

/**
 * @deprecated Use `githubLinkStart()` instead.
 * URL that begins the "bind GitHub to my account" OAuth dance.
 * Hard navigation via `window.location.href` does not send the bearer token,
 * causing a 401 on the authenticated `/link/start` endpoint.
 */
export function githubLinkStartUrl(redirect?: string): string {
  const qs = redirect ? `?redirect=${encodeURIComponent(redirect)}` : '';
  return `/api/auth/oauth/github/link/start${qs}`;
}

/**
 * POST /api/auth/oauth/github/link/init — authenticated via axios bearer header.
 * Returns the GitHub authorize URL (with signed state nonce) so the frontend can
 * do `window.location.href = authorize_url` without losing the JWT in transit.
 */
export async function githubLinkStart(redirect?: string): Promise<{ authorize_url: string }> {
  const params = redirect ? { redirect } : undefined;
  const { data } = await http.post<{ authorize_url: string }>(
    '/auth/oauth/github/link/init',
    undefined,
    { params },
  );
  return data;
}

/** Detach the current user's GitHub identity. Returns 204 on success. */
export async function githubUnlink(): Promise<void> {
  await http.post('/auth/oauth/github/unlink');
}

/**
 * Set a password for a GitHub-only user (has_password=false). Backend
 * refuses if the user already has a password.
 */
export async function githubSetPassword(new_password: string): Promise<void> {
  await http.post('/auth/oauth/github/set-password', { new_password });
}

// ---------------------------------------------------------------------------
// Active sessions — Settings > Sessions panel (issue #31).
// ---------------------------------------------------------------------------

/** GET /api/auth/sessions — caller's currently-valid JWTs. */
export async function listSessions(): Promise<ActiveSession[]> {
  const { data } = await http.get<ActiveSession[]>('/auth/sessions');
  return data;
}

/**
 * POST /api/auth/sessions/{jti}/revoke — revoke one of the caller's JWTs.
 *
 * Backend returns 404 for a jti that doesn't exist or belongs to someone
 * else (indistinguishable from "already expired"); callers should treat
 * 404 as "already gone" rather than a hard error.
 */
export async function revokeSession(jti: string): Promise<{ ok: boolean; detail?: string }> {
  const { data } = await http.post<{ ok: boolean; detail?: string }>(
    `/auth/sessions/${encodeURIComponent(jti)}/revoke`,
  );
  return data;
}

// ---------------------------------------------------------------------------
// /api/users/me/preferences — per-user UI knobs (hide_demo, locale, ...).
// Kept next to the auth wrappers because the preferences endpoint lives
// adjacent to /auth/me in terms of UX (same page renders both).
// ---------------------------------------------------------------------------

/** Shape of the per-user preferences payload. */
export interface UserPreferences {
  hide_demo: boolean;
  preferred_locale: string;
}

/** GET /api/users/me/preferences */
export async function getPreferences(): Promise<UserPreferences> {
  const { data } = await http.get<UserPreferences>('/users/me/preferences');
  return data;
}

/** PATCH /api/users/me/preferences — PATCH semantics: omit fields to keep them. */
export async function updatePreferences(
  body: Partial<UserPreferences>,
): Promise<UserPreferences> {
  const { data } = await http.patch<UserPreferences>(
    '/users/me/preferences',
    body,
  );
  return data;
}
