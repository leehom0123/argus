// Public-link CRUD + anonymous read endpoints.
//
// Two audiences:
//   1. Authenticated owners managing their public links — use `http` (JWT OK).
//   2. Anonymous viewers at /public/<slug> — must NOT send Authorization.
//      We use a bare axios instance (`anonHttp`) so nothing leaks even if a
//      stale JWT sits in localStorage.

import axios from 'axios';
import { http } from './client';
import type {
  Batch,
  EpochPoint,
  GenericSuccess,
  Job,
  PublicShare,
} from '../types';

/** Separate axios instance with NO request interceptors — never sends a token. */
const anonHttp = axios.create({
  baseURL: '/api',
  timeout: 10_000,
});

// -------- Owner-side management (authenticated) --------

export interface CreatePublicShareBody {
  /** ISO datetime; omit for "never expires" (subject to backend policy). */
  expires_at?: string | null;
}

/**
 * Create a public share for the given batch. Backend returns `{slug, url, expires_at}`.
 * A single batch may have multiple active slugs (see BatchShare dialog).
 */
export async function createPublicShare(
  batchId: string,
  body: CreatePublicShareBody = {},
): Promise<PublicShare> {
  const { data } = await http.post<PublicShare>(
    `/batches/${encodeURIComponent(batchId)}/public-share`,
    body,
  );
  return data;
}

export async function revokePublicShare(
  batchId: string,
  slug: string,
): Promise<GenericSuccess> {
  const { data } = await http.delete<GenericSuccess>(
    `/batches/${encodeURIComponent(batchId)}/public-share/${encodeURIComponent(slug)}`,
  );
  return data;
}

/**
 * List public shares owned by a specific batch. BACKEND-C exposes this via GET
 * on the same URL as POST.
 */
export async function listBatchPublicShares(batchId: string): Promise<PublicShare[]> {
  const { data } = await http.get<PublicShare[]>(
    `/batches/${encodeURIComponent(batchId)}/public-shares`,
  );
  return data;
}

/**
 * Convenience: enumerate all public shares owned by the current user by asking
 * the backend to aggregate. If BACKEND-C doesn't ship this endpoint, callers
 * fall back to composing from each batch (see Shares.vue).
 */
export async function listMyPublicShares(): Promise<PublicShare[]> {
  const { data } = await http.get<PublicShare[]>('/public-shares/mine');
  return data;
}

// -------- Anonymous (public-link visitor) --------

/** The backend returns a composite payload here; we type it loosely. */
export interface PublicBatchPayload {
  batch: Batch;
  /** If the backend inlines jobs, surface them; otherwise fetch via listPublicJobs. */
  jobs?: Job[];
  /** slug metadata (view count, expiry) */
  public_share?: PublicShare;
}

export async function getPublicBatch(slug: string): Promise<PublicBatchPayload> {
  const { data } = await anonHttp.get<PublicBatchPayload>(
    `/public/${encodeURIComponent(slug)}`,
  );
  return data;
}

export async function listPublicJobs(slug: string): Promise<Job[]> {
  const { data } = await anonHttp.get<Job[]>(
    `/public/${encodeURIComponent(slug)}/jobs`,
  );
  return data;
}

export async function getPublicJob(slug: string, jobId: string): Promise<Job> {
  const { data } = await anonHttp.get<Job>(
    `/public/${encodeURIComponent(slug)}/jobs/${encodeURIComponent(jobId)}`,
  );
  return data;
}

export async function getPublicJobEpochs(slug: string, jobId: string): Promise<EpochPoint[]> {
  const { data } = await anonHttp.get<EpochPoint[]>(
    `/public/${encodeURIComponent(slug)}/jobs/${encodeURIComponent(jobId)}/epochs`,
  );
  return data;
}

// -------- Anonymous public-demo projects --------

export interface PublicProjectSummary {
  project: string;
  description: string | null;
  published_at: string | null;
  n_batches: number;
}

export interface PublicProjectDetail {
  project: string;
  description: string | null;
  published_at: string | null;
  n_batches: number;
  running_batches: number;
  jobs_done: number;
  jobs_failed: number;
  failure_rate: number | null;
  gpu_hours: number;
  first_event_at: string | null;
  last_event_at: string | null;
}

export interface PublicProjectBatch {
  batch_id: string;
  project: string;
  host: string | null;
  status: string | null;
  n_total: number | null;
  n_done: number;
  n_failed: number;
  start_time: string | null;
  end_time: string | null;
}

export async function listPublicProjects(): Promise<PublicProjectSummary[]> {
  const { data } = await anonHttp.get<PublicProjectSummary[]>('/public/projects');
  return data;
}

export async function getPublicProject(project: string): Promise<PublicProjectDetail> {
  const { data } = await anonHttp.get<PublicProjectDetail>(
    `/public/projects/${encodeURIComponent(project)}`,
  );
  return data;
}

export async function getPublicProjectLeaderboard(
  project: string,
  metric = 'MSE',
): Promise<import('../types').LeaderboardRow[]> {
  const { data } = await anonHttp.get<import('../types').LeaderboardRow[]>(
    `/public/projects/${encodeURIComponent(project)}/leaderboard`,
    { params: { metric } },
  );
  return data;
}

export async function getPublicProjectMatrix(
  project: string,
  metric = 'MSE',
): Promise<{
  metric: string;
  rows: string[];
  cols: string[];
  values: (number | null)[][];
  batch_ids: (string[] | null)[][];
}> {
  const { data } = await anonHttp.get(
    `/public/projects/${encodeURIComponent(project)}/matrix`,
    { params: { metric } },
  );
  return data as {
    metric: string;
    rows: string[];
    cols: string[];
    values: (number | null)[][];
    batch_ids: (string[] | null)[][];
  };
}

export async function getPublicProjectActiveBatches(
  project: string,
): Promise<import('../types').ActiveBatchCard[]> {
  const { data } = await anonHttp.get<import('../types').ActiveBatchCard[]>(
    `/public/projects/${encodeURIComponent(project)}/active-batches`,
  );
  return data;
}

export async function getPublicProjectResources(project: string): Promise<{
  project: string;
  gpu_hours: number;
  jobs_completed: number;
  avg_job_minutes: number | null;
  hourly_heatmap: number[][];
  host_distribution: Record<string, number>;
}> {
  const { data } = await anonHttp.get(
    `/public/projects/${encodeURIComponent(project)}/resources`,
  );
  return data as {
    project: string;
    gpu_hours: number;
    jobs_completed: number;
    avg_job_minutes: number | null;
    hourly_heatmap: number[][];
    host_distribution: Record<string, number>;
  };
}

export async function getPublicProjectBatches(
  project: string,
): Promise<PublicProjectBatch[]> {
  const { data } = await anonHttp.get<PublicProjectBatch[]>(
    `/public/projects/${encodeURIComponent(project)}/batches`,
  );
  return data;
}
