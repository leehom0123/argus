// Compare-pool payload — requirements §16.7 / §17.
//
// Accepts 2-4 batch IDs in the `batches` query param (comma-separated per the
// REST spec). Returns per-batch loss curves, aligned metrics, and per-batch
// matrices so the client can do side-by-side + diff views without a second
// round-trip.

import { http } from './client';
import type { CompareData } from '../types';

export async function getCompare(batchIds: string[]): Promise<CompareData> {
  const { data } = await http.get<CompareData>('/compare', {
    params: { batches: batchIds.join(',') },
  });
  return data;
}
