/**
 * /api/notifications — in-app notification bell API client.
 */
import { http } from './client';

export interface AppNotification {
  id: number;
  batch_id: string | null;
  rule_id: string;
  severity: 'info' | 'warn' | 'error';
  title: string;
  body: string;
  created_at: string;
  read_at: string | null;
}

export interface ListNotificationsParams {
  limit?: number;
  unread_only?: boolean;
}

export async function listNotifications(
  params: ListNotificationsParams = {},
): Promise<AppNotification[]> {
  const { data } = await http.get<AppNotification[]>('/notifications', { params });
  return data;
}

export async function ackNotification(id: number): Promise<void> {
  await http.post(`/notifications/${id}/ack`);
}

export async function markAllRead(): Promise<void> {
  await http.post('/notifications/mark_all_read');
}

export async function deleteNotification(id: number): Promise<void> {
  await http.delete(`/notifications/${id}`);
}
