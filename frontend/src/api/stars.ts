// Star (favorite) CRUD for projects and batches — requirements §16.6 / §17.
//
// Backend maintains a composite key (target_type, target_id). DELETE takes the
// pair in the URL rather than a body so browsers / caches play nice. The
// GET returns every star for the current user.

import { http } from './client';
import type { GenericSuccess, Star } from '../types';

export async function listStars(): Promise<Star[]> {
  const { data } = await http.get<Star[]>('/stars');
  return data;
}

export interface AddStarBody {
  target_type: 'project' | 'batch';
  target_id: string;
}

export async function addStar(body: AddStarBody): Promise<Star> {
  const { data } = await http.post<Star>('/stars', body);
  return data;
}

export async function removeStar(
  targetType: 'project' | 'batch',
  targetId: string,
): Promise<GenericSuccess> {
  const { data } = await http.delete<GenericSuccess>(
    `/stars/${encodeURIComponent(targetType)}/${encodeURIComponent(targetId)}`,
  );
  return data;
}
