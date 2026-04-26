// Batch-share and project-share CRUD.
//
// These hit the BACKEND-C endpoints documented in requirements §7.5.
// The "shared by me" aggregation has no dedicated endpoint in MVP — we compose
// it on the client by enumerating all of the current user's batches and
// fetching per-batch shares. See Shares.vue for the aggregation logic.

import { http } from './client';
import type { BatchShare, GenericSuccess, ProjectShare, SharePermission } from '../types';

// -------- Batch-level shares --------

export async function listBatchShares(batchId: string): Promise<BatchShare[]> {
  const { data } = await http.get<BatchShare[]>(
    `/batches/${encodeURIComponent(batchId)}/shares`,
  );
  return data;
}

export interface AddBatchShareBody {
  grantee_username: string;
  permission: SharePermission;
}

export async function addBatchShare(
  batchId: string,
  body: AddBatchShareBody,
): Promise<BatchShare> {
  const { data } = await http.post<BatchShare>(
    `/batches/${encodeURIComponent(batchId)}/shares`,
    body,
  );
  return data;
}

export async function removeBatchShare(
  batchId: string,
  granteeId: number,
): Promise<GenericSuccess> {
  const { data } = await http.delete<GenericSuccess>(
    `/batches/${encodeURIComponent(batchId)}/shares/${granteeId}`,
  );
  return data;
}

// -------- Project-level shares --------

export async function listProjectShares(): Promise<ProjectShare[]> {
  // Returns every project share created by the authenticated user (per §7.5).
  const { data } = await http.get<ProjectShare[]>('/projects/shares');
  return data;
}

export interface AddProjectShareBody {
  project: string;
  grantee_username: string;
  permission: SharePermission;
}

export async function addProjectShare(body: AddProjectShareBody): Promise<ProjectShare> {
  const { data } = await http.post<ProjectShare>('/projects/shares', body);
  return data;
}

export async function removeProjectShare(
  project: string,
  granteeId: number,
): Promise<GenericSuccess> {
  const { data } = await http.delete<GenericSuccess>(
    `/projects/shares/${encodeURIComponent(project)}/${granteeId}`,
  );
  return data;
}
