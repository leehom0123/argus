// Project-level endpoints (requirements §17).
//
// All project IDs are free-form strings (the `source.project` attribute from
// event_v1); URL-encode on the way out so `/` or spaces in names don't break
// the route. The backend responses lean on optional fields — anything we
// haven't locked in with BACKEND-R2 gets a permissive type so typecheck
// passes against today's stubs.

import { http } from './client';
import type {
  ActiveBatchCard,
  LeaderboardRow,
  MatrixData,
  ProjectDetail,
  ProjectResourcesData,
  ProjectSummary,
  BatchHealth,
  BatchETA,
} from '../types';

export interface ListProjectsParams {
  scope?: 'mine' | 'shared' | 'all';
  starred?: boolean;
}

export async function listProjects(params: ListProjectsParams = {}): Promise<ProjectSummary[]> {
  const { data } = await http.get<ProjectSummary[]>('/projects', { params });
  return data;
}

export async function getProject(project: string): Promise<ProjectDetail> {
  const { data } = await http.get<ProjectDetail>(`/projects/${encodeURIComponent(project)}`);
  return data;
}

export async function getProjectActiveBatches(project: string): Promise<ActiveBatchCard[]> {
  const { data } = await http.get<ActiveBatchCard[]>(
    `/projects/${encodeURIComponent(project)}/active-batches`,
  );
  return data;
}

export interface LeaderboardParams {
  /** Optional sort-order metric; server ranks by min(metric). Defaults to MSE. */
  metric?: string;
  limit?: number;
}

export async function getProjectLeaderboard(
  project: string,
  params: LeaderboardParams = {},
): Promise<LeaderboardRow[]> {
  const { data } = await http.get<LeaderboardRow[]>(
    `/projects/${encodeURIComponent(project)}/leaderboard`,
    { params },
  );
  return data;
}

/** Raw shape that the live backend emits for the matrix endpoint. */
interface MatrixRaw {
  metric: string;
  /** Backend names: rows/cols/values */
  rows?: string[];
  cols?: string[];
  values?: (number | null)[][];
  /** Parallel to values[][]: batch IDs per cell (newest-first, up to 3). */
  batch_ids?: (string[] | null)[][];
  /** Already-normalised shape (future-proof or post-schema-update) */
  models?: string[];
  datasets?: string[];
  cells?: MatrixData['cells'];
}

export async function getProjectMatrix(
  project: string,
  metric = 'MSE',
): Promise<MatrixData> {
  const { data } = await http.get<MatrixRaw>(
    `/projects/${encodeURIComponent(project)}/matrix`,
    { params: { metric } },
  );
  // Normalise live API shape {rows, cols, values[][], batch_ids[][]} → {models, datasets, cells[], batchIds[]}
  if (data.rows && data.cols && data.values && !data.models) {
    const cells: MatrixData['cells'] = [];
    const batchIds: (string[] | null)[] = [];
    data.rows.forEach((model, r) => {
      (data.cols as string[]).forEach((dataset, c) => {
        cells.push({ model, dataset, value: data.values![r][c] ?? null });
        batchIds.push(data.batch_ids?.[r]?.[c] ?? null);
      });
    });
    return { metric: data.metric, models: data.rows, datasets: data.cols, cells, batchIds };
  }
  return data as MatrixData;
}

/** Raw shape the live backend emits for the resources endpoint. */
interface ProjectResourcesRaw {
  gpu_hours?: number | null;
  jobs_completed?: number | null;
  avg_job_minutes?: number | null;
  hourly_heatmap?: number[][] | null;
  host_distribution?: Record<string, number> | null;
  // Already-normalised fields (future-proof)
  total_gpu_hours?: number | null;
  by_host?: ProjectResourcesData['by_host'];
  timeseries?: ProjectResourcesData['timeseries'];
}

export async function getProjectResources(project: string): Promise<ProjectResourcesData> {
  const { data } = await http.get<ProjectResourcesRaw>(
    `/projects/${encodeURIComponent(project)}/resources`,
  );
  // Normalise live API shape to what the template expects
  const by_host: ProjectResourcesData['by_host'] = data.by_host
    ?? (data.host_distribution
      ? Object.entries(data.host_distribution).map(([host, gpu_hours]) => ({ host, gpu_hours }))
      : null);
  return {
    total_gpu_hours: data.total_gpu_hours ?? data.gpu_hours ?? null,
    by_host,
    hourly_heatmap: data.hourly_heatmap ?? null,
    timeseries: data.timeseries ?? undefined,
  };
}

// -------- Batch health + ETA (hit via the batch card) --------

export async function getBatchHealth(batchId: string): Promise<BatchHealth> {
  const { data } = await http.get<BatchHealth>(
    `/batches/${encodeURIComponent(batchId)}/health`,
  );
  return data;
}

export async function getBatchEta(batchId: string): Promise<BatchETA> {
  const { data } = await http.get<BatchETA>(
    `/batches/${encodeURIComponent(batchId)}/eta`,
  );
  return data;
}
