// Pin (compare-pool) CRUD — requirements §16.7 / §17.
//
// Hard cap of 4 pins per user is enforced both in UI (PinButton + store) and
// in the backend (400 on overflow). We still surface the backend error so
// the client can react even if it missed an add/remove in flight.

import { http } from './client';
import type { GenericSuccess, Pin } from '../types';

export async function listPins(): Promise<Pin[]> {
  const { data } = await http.get<Pin[]>('/pins');
  return data;
}

export interface AddPinBody {
  batch_id: string;
}

export async function addPin(body: AddPinBody): Promise<Pin> {
  const { data } = await http.post<Pin>('/pins', body);
  return data;
}

export async function removePin(batchId: string): Promise<GenericSuccess> {
  const { data } = await http.delete<GenericSuccess>(
    `/pins/${encodeURIComponent(batchId)}`,
  );
  return data;
}
