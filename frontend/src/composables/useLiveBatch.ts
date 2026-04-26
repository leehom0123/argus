/**
 * SSE subscription for live batch/job updates.
 *
 * BACKEND-D exposes `GET /api/events/stream?batch_id=X&token=JWT`.
 * We pass the JWT in the query string because EventSource can't set
 * Authorization headers; the backend validates both header and query forms.
 *
 * Usage:
 *
 *   const { connected, error } = useLiveBatch(batchIdRef, {
 *     onJobEpoch: (batchId, jobId, epoch) => { ... },
 *     onJobStatus: (batchId, jobId, status) => { ... },
 *     onBatchStatus: (batchId, status) => { ... },
 *   });
 *
 * Behaviour:
 *   - Opens only while `batchIdRef.value` is a non-empty string. Pass null/
 *     empty to turn the subscription off (or pass a ref that becomes empty
 *     when the batch stops running).
 *   - Auto-reconnect with exponential backoff (up to 30s), infinite retries.
 *   - Tears down on unmount.
 *
 * The response envelope is assumed to be one of the schema v1.0 events
 * serialised as JSON. We try to route to the handler pair; unknown types
 * are dropped silently.
 */

import { onBeforeUnmount, watch, ref, type Ref } from 'vue';

export interface UseLiveBatchHandlers {
  onJobEpoch?: (batchId: string, jobId: string, epoch: unknown) => void;
  onJobStatus?: (batchId: string, jobId: string, status: string) => void;
  onBatchStatus?: (batchId: string, status: string) => void;
}

interface LiveSubscription {
  connected: Ref<boolean>;
  error: Ref<string | null>;
  close: () => void;
}

const LS_TOKEN_KEY = 'argus.access_token';

export function useLiveBatch(
  batchId: string | Ref<string | null>,
  handlers: UseLiveBatchHandlers = {},
): LiveSubscription {
  const connected = ref(false);
  const error = ref<string | null>(null);

  let source: EventSource | null = null;
  let reconnectTimer: number | null = null;
  let backoff = 1000;

  const idRef = typeof batchId === 'string' ? ref(batchId) : batchId;

  function teardown() {
    if (source) {
      try {
        source.close();
      } catch {
        // ignore
      }
      source = null;
    }
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    connected.value = false;
  }

  function connect() {
    teardown();
    const id = idRef.value;
    if (!id) return;
    const token = localStorage.getItem(LS_TOKEN_KEY) ?? '';
    if (!token) {
      error.value = 'no auth token';
      return;
    }
    const url = `/api/events/stream?batch_id=${encodeURIComponent(id)}&token=${encodeURIComponent(token)}`;
    try {
      source = new EventSource(url);
    } catch (e) {
      error.value = (e as Error).message;
      scheduleReconnect();
      return;
    }

    source.onopen = () => {
      connected.value = true;
      error.value = null;
      backoff = 1000;
    };

    source.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data) as {
          event_type?: string;
          batch_id?: string;
          job_id?: string;
          status?: string;
          data?: unknown;
        };
        const type = payload.event_type ?? '';
        const bid = payload.batch_id ?? id;
        const jid = payload.job_id ?? '';
        if (type === 'job_epoch' && jid) {
          handlers.onJobEpoch?.(bid, jid, payload.data ?? payload);
        } else if (
          (type === 'job_done' || type === 'job_failed' || type === 'job_start') &&
          jid
        ) {
          const status =
            payload.status ??
            (type === 'job_done' ? 'done' : type === 'job_failed' ? 'failed' : 'running');
          handlers.onJobStatus?.(bid, jid, status);
        } else if (type === 'batch_done' || type === 'batch_failed' || type === 'batch_start') {
          const status =
            payload.status ??
            (type === 'batch_done' ? 'done' : type === 'batch_failed' ? 'failed' : 'running');
          handlers.onBatchStatus?.(bid, status);
        }
      } catch {
        // malformed frame — skip
      }
    };

    source.onerror = () => {
      connected.value = false;
      error.value = 'stream disconnected';
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    teardown();
    if (!idRef.value) return;
    const delay = Math.min(30_000, backoff);
    backoff = Math.min(30_000, backoff * 2);
    reconnectTimer = window.setTimeout(connect, delay);
  }

  watch(
    idRef,
    () => {
      if (idRef.value) connect();
      else teardown();
    },
    { immediate: true },
  );

  onBeforeUnmount(teardown);

  return { connected, error, close: teardown };
}
