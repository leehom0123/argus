/**
 * useMultiSSE — multiplexed SSE client composable.
 *
 * Wraps ``GET /api/sse?channels=batch:X,job:X:Y,dashboard`` (introduced
 * in v0.2) so a page can subscribe to several logical streams over a
 * single ``EventSource`` instead of opening one per stream. Replaces
 * the N+2 connection pattern on BatchDetail (1 batch + N jobs + 1
 * dashboard) with one.
 *
 * Each frame from the backend includes a ``channel`` field naming the
 * channel selector that matched it. We use that to dispatch to the
 * caller's per-channel handler — handlers don't need to second-guess
 * which subscription a frame came from.
 *
 * Usage:
 *
 *   const { connected, error, close } = useMultiSSE(channelsRef, {
 *     onMessage: (channel, eventType, payload) => { ... },
 *     onChannel: {
 *       'batch:abc': (eventType, payload) => { ... },
 *       'job:abc:j1': (eventType, payload) => { ... },
 *     },
 *   });
 *
 * Behaviour:
 *   - Re-opens whenever ``channelsRef.value`` changes (the URL changes,
 *     so the connection has to reconnect anyway — keeping it implicit
 *     in this composable matches the single-channel ``useLiveBatch``
 *     ergonomics).
 *   - Auto-reconnect with exponential backoff (capped at 30s).
 *   - ``hello`` / ``keepalive`` frames are routed via ``onMessage``
 *     under the special channel names ``__hello__`` / ``__keepalive__``
 *     so callers can verify the subscription landed.
 *
 * The composable does NOT validate the channel selectors client-side
 * beyond skipping empties — the backend returns 400 on malformed input,
 * and a thin client lets the wire format evolve without composable
 * churn.
 */

import { onBeforeUnmount, watch, ref, type Ref } from 'vue';

/** Per-channel handler. Receives the event_type + the parsed payload (the full frame minus the ``channel`` key). */
export type MultiSSEHandler = (eventType: string, payload: Record<string, unknown>) => void;

export interface UseMultiSSEHandlers {
  /** Catch-all — fires for every frame including ``hello`` / ``keepalive``. */
  onMessage?: (channel: string, eventType: string, payload: Record<string, unknown>) => void;
  /** Per-channel dispatch. Channel name must match the selector exactly (e.g. ``"batch:abc"``). */
  onChannel?: Record<string, MultiSSEHandler>;
  /** Open / close lifecycle hooks for the underlying EventSource. */
  onOpen?: () => void;
  onError?: (msg: string) => void;
}

export interface UseMultiSSE {
  connected: Ref<boolean>;
  error: Ref<string | null>;
  /** Tear the connection down (also runs on unmount). */
  close: () => void;
}

const LS_TOKEN_KEY = 'argus.access_token';

/**
 * Open a multiplexed SSE connection.
 *
 * @param channels - either an array literal or a ref. Empty array (or
 *   ref pointing at one) tears the connection down — same convention as
 *   ``useLiveBatch`` accepting an empty batch_id.
 */
export function useMultiSSE(
  channels: string[] | Ref<string[]>,
  handlers: UseMultiSSEHandlers = {},
): UseMultiSSE {
  const connected = ref(false);
  const error = ref<string | null>(null);

  let source: EventSource | null = null;
  let reconnectTimer: number | null = null;
  let backoff = 1000;

  const channelsRef = Array.isArray(channels) ? ref(channels) : channels;

  function teardown(): void {
    if (source) {
      try {
        source.close();
      } catch {
        // EventSource.close() is supposed to be idempotent, but some
        // jsdom shims throw on double-close. Swallow — we're tearing
        // down anyway.
      }
      source = null;
    }
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    connected.value = false;
  }

  function dispatch(frame: { channel?: string; event_type?: string } & Record<string, unknown>, eventType: string): void {
    // The backend tags every frame with a ``channel`` field. ``hello``
    // and ``keepalive`` originate from the multiplex generator itself
    // and don't carry a channel, so we route them under reserved
    // sentinel names so callers can subscribe (e.g. for a ready
    // indicator) without parsing event_type strings.
    const channel = typeof frame.channel === 'string'
      ? frame.channel
      : eventType === 'hello'
      ? '__hello__'
      : eventType === 'keepalive'
      ? '__keepalive__'
      : '__unknown__';

    handlers.onMessage?.(channel, eventType, frame);

    const perChannel = handlers.onChannel?.[channel];
    if (perChannel) {
      perChannel(eventType, frame);
    }
  }

  function buildUrl(token: string, list: string[]): string {
    return `/api/sse?channels=${encodeURIComponent(list.join(','))}&token=${encodeURIComponent(token)}`;
  }

  // The set of event types we addEventListener for. Native EventSource
  // routes typed frames (``event: foo``) to listeners registered for
  // that name; ``onmessage`` only fires for unnamed frames. The backend
  // emits typed frames for every event, so we need an explicit listener
  // per event_type — but we don't know the universe up front. Keeping
  // the list scoped to "things the backend actually emits today" is
  // good enough; new event types just need adding here.
  const KNOWN_EVENT_TYPES: string[] = [
    'hello',
    'keepalive',
    'job_epoch',
    'job_start',
    'job_done',
    'job_failed',
    'log_line',
    'batch_start',
    'batch_done',
    'batch_failed',
    'resource_snapshot',
  ];

  function connect(): void {
    teardown();
    const list = channelsRef.value.filter((s) => s && s.trim().length > 0);
    if (list.length === 0) return;

    const token = localStorage.getItem(LS_TOKEN_KEY) ?? '';
    if (!token) {
      error.value = 'no auth token';
      handlers.onError?.(error.value);
      return;
    }

    const url = buildUrl(token, list);
    try {
      source = new EventSource(url);
    } catch (e) {
      error.value = (e as Error).message;
      handlers.onError?.(error.value);
      scheduleReconnect();
      return;
    }

    source.onopen = () => {
      connected.value = true;
      error.value = null;
      backoff = 1000;
      handlers.onOpen?.();
    };

    const handle = (ev: MessageEvent, eventType: string) => {
      try {
        const payload = JSON.parse(ev.data) as Record<string, unknown>;
        dispatch(payload, eventType);
      } catch {
        // Malformed JSON shouldn't kill the listener — just skip.
      }
    };

    for (const ev of KNOWN_EVENT_TYPES) {
      source.addEventListener(ev, (e) => handle(e as MessageEvent, ev));
    }
    // Fallback for un-typed frames (event: <empty>). The backend should
    // always emit a type, but this guards against future schema gaps.
    source.onmessage = (ev) => handle(ev, 'message');

    source.onerror = () => {
      connected.value = false;
      error.value = 'stream disconnected';
      handlers.onError?.(error.value);
      scheduleReconnect();
    };
  }

  function scheduleReconnect(): void {
    teardown();
    if (channelsRef.value.length === 0) return;
    const delay = Math.min(30_000, backoff);
    backoff = Math.min(30_000, backoff * 2);
    reconnectTimer = window.setTimeout(connect, delay);
  }

  watch(
    channelsRef,
    () => {
      // Channel set change → reconnect. We can't add channels to a
      // running EventSource (the URL is part of the request); reopening
      // is the simplest correct path.
      if (channelsRef.value.length > 0) connect();
      else teardown();
    },
    { immediate: true, deep: true },
  );

  onBeforeUnmount(teardown);

  return { connected, error, close: teardown };
}
