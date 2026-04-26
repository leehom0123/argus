// Dashboard aggregation endpoint (requirements §17).
//
// A single round-trip that covers counters + top projects + activity feed
// + host summary + notifications. The backend intentionally packs all five
// so the UI avoids an N+1 on first paint. We keep the response type lenient
// so the contract can settle during QA without a type-check churn here.

import { http } from './client';
import type { DashboardData } from '../types';

export interface DashboardParams {
  /** Restrict to `mine` (default), `shared`, or `all` (admin only). */
  scope?: 'mine' | 'shared' | 'all';
}

export async function getDashboard(params: DashboardParams = {}): Promise<DashboardData> {
  const { data } = await http.get<DashboardData>('/dashboard', { params });
  return data;
}
