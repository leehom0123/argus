<script setup lang="ts">
/**
 * LogTailDrawer — live log tail for a batch or a specific job.
 *
 * Wraps the SSE endpoint ``/api/(batches|jobs)/.../logs/stream`` in an
 * ``<a-drawer>`` so users stop SSHing to the machine to tail ``main.log``
 * (PM roadmap #4). Deliberately lean: auto-scroll, level coloring, ring
 * buffer, reconnect on error. We do NOT replace the existing polling
 * Logs tab — the two coexist; this is the "interactive" surface.
 *
 * Props:
 *   open      — v-model-able visibility flag
 *   batchId   — required; selects the batch-scoped endpoint by default
 *   jobId     — optional; switches to the job-scoped endpoint
 *
 * Design notes:
 *   - EventSource can't set Authorization headers, so the JWT / API
 *     token is passed via ``?token=`` (same shim as other SSE surfaces).
 *   - Ring buffer capped at TAIL_LIMIT lines — overflow drops oldest so
 *     long-running tails don't balloon DOM size.
 *   - Auto-scroll to bottom as new lines arrive, but only when the user
 *     was already at the bottom (leaves manual scrollback alone).
 *   - ``displaced`` frames from the server (another tab took over) are
 *     surfaced as a banner instead of a silent disconnect.
 */
import { ref, watch, nextTick, onBeforeUnmount, computed } from 'vue';
import { useI18n } from 'vue-i18n';

const { t } = useI18n();

const props = defineProps<{
  open: boolean;
  batchId: string;
  jobId?: string | null;
}>();

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void;
}>();

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

// Cap chosen to match the spec (5000 lines). Anything higher and a long
// batch can push the DOM into the tens-of-thousands of <div> range which
// ant-design scroll containers handle but react slowly to.
const TAIL_LIMIT = 5000;
const LS_TOKEN_KEY = 'argus.access_token';

type Level = 'error' | 'warning' | 'info' | 'debug' | string;
interface LogEntry {
  ts: string;         // ISO timestamp from the event envelope
  level: Level;
  line: string;
  jobId: string | null;
}

const lines = ref<LogEntry[]>([]);
const connected = ref(false);
const displaced = ref(false);
const errorMsg = ref<string | null>(null);
const autoScroll = ref(true);
const scrollEl = ref<HTMLElement | null>(null);

let source: EventSource | null = null;
let reconnectTimer: number | null = null;
let backoff = 1000;

const drawerTitle = computed(() =>
  props.jobId
    ? t('component_log_tail_drawer.title_job', {
      batch: props.batchId,
      job: props.jobId,
    })
    : t('component_log_tail_drawer.title_batch', { batch: props.batchId }),
);

// ---------------------------------------------------------------------------
// SSE connection lifecycle
// ---------------------------------------------------------------------------

function endpointUrl(token: string): string {
  const tokenQs = `token=${encodeURIComponent(token)}`;
  if (props.jobId) {
    // Job ids aren't globally unique — they're namespaced by batch, so
    // the server route is composite; mirror that in the client.
    return `/api/jobs/${encodeURIComponent(props.batchId)}/${encodeURIComponent(props.jobId)}/logs/stream?${tokenQs}`;
  }
  return `/api/batches/${encodeURIComponent(props.batchId)}/logs/stream?${tokenQs}`;
}

function teardown(): void {
  if (source) {
    try {
      source.close();
    } catch {
      // already closed — ignore
    }
    source = null;
  }
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  connected.value = false;
}

function scheduleReconnect(): void {
  teardown();
  if (!props.open) return;
  const delay = Math.min(30_000, backoff);
  backoff = Math.min(30_000, backoff * 2);
  reconnectTimer = window.setTimeout(connect, delay);
}

function connect(): void {
  teardown();
  if (!props.open || !props.batchId) return;
  const token = localStorage.getItem(LS_TOKEN_KEY) ?? '';
  if (!token) {
    errorMsg.value = t('component_log_tail_drawer.error_no_token');
    return;
  }

  try {
    source = new EventSource(endpointUrl(token));
  } catch (e) {
    errorMsg.value = (e as Error).message;
    scheduleReconnect();
    return;
  }

  source.addEventListener('hello', () => {
    connected.value = true;
    errorMsg.value = null;
    displaced.value = false;
    backoff = 1000;
  });

  source.addEventListener('log_line', (ev) => {
    try {
      const payload = JSON.parse((ev as MessageEvent).data) as {
        timestamp?: string;
        job_id?: string | null;
        data?: { level?: string; line?: string; message?: string };
      };
      const data = payload.data ?? {};
      pushLine({
        ts: payload.timestamp ?? '',
        level: (data.level as Level) ?? 'info',
        // Reporters emit either ``line`` (preferred) or ``message`` (older
        // clients) — accept both so we don't drop traffic mid-rollout.
        line: data.line ?? data.message ?? '',
        jobId: payload.job_id ?? null,
      });
    } catch {
      // malformed frame — skip rather than crash the tail
    }
  });

  source.addEventListener('displaced', () => {
    displaced.value = true;
    teardown();
  });

  // ``keepalive`` frames are silent by design — we only use them to
  // keep intermediaries from timing out; no UI update needed.

  source.onerror = () => {
    connected.value = false;
    if (!displaced.value) {
      errorMsg.value = t('component_log_tail_drawer.error_disconnected');
      scheduleReconnect();
    }
  };
}

// ---------------------------------------------------------------------------
// Ring buffer + autoscroll
// ---------------------------------------------------------------------------

function pushLine(entry: LogEntry): void {
  lines.value.push(entry);
  if (lines.value.length > TAIL_LIMIT) {
    // Splice in place so any v-for bindings stay stable on the tail.
    lines.value.splice(0, lines.value.length - TAIL_LIMIT);
  }
  if (autoScroll.value) {
    void nextTick(() => {
      const el = scrollEl.value;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }
}

function handleScroll(): void {
  const el = scrollEl.value;
  if (!el) return;
  // A 4-px slack absorbs subpixel differences from high-DPR rendering.
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 4;
  autoScroll.value = atBottom;
}

function clearBuffer(): void {
  lines.value = [];
}

function reconnectManually(): void {
  errorMsg.value = null;
  displaced.value = false;
  backoff = 1000;
  connect();
}

// ---------------------------------------------------------------------------
// Open/close + identity-change lifecycle
// ---------------------------------------------------------------------------

watch(
  () => [props.open, props.batchId, props.jobId],
  ([open]) => {
    if (open) {
      connect();
    } else {
      teardown();
    }
  },
  { immediate: true },
);

onBeforeUnmount(teardown);

function onDrawerClose(): void {
  emit('update:open', false);
}

// ---------------------------------------------------------------------------
// Level → color mapping (kept local so the drawer doesn't depend on a
// theme composable; AntD token colors picked to match StatusTag).
// ---------------------------------------------------------------------------

function levelColor(level: Level): string {
  switch ((level ?? '').toLowerCase()) {
    case 'error':
    case 'critical':
    case 'fatal':
      return '#ff4d4f';
    case 'warn':
    case 'warning':
      return '#faad14';
    case 'debug':
    case 'trace':
      return 'rgba(255,255,255,0.45)';
    default:
      return 'rgba(255,255,255,0.75)';
  }
}
</script>

<template>
  <a-drawer
    :open="props.open"
    :title="drawerTitle"
    :width="720"
    placement="right"
    :body-style="{
      padding: 0,
      background: '#1e1e1e',
      display: 'flex',
      flexDirection: 'column',
    }"
    @close="onDrawerClose"
  >
    <template #extra>
      <a-space :size="8">
        <a-tag v-if="connected" color="green">
          {{ t('component_log_tail_drawer.status_live') }}
        </a-tag>
        <a-tag v-else-if="displaced" color="orange">
          {{ t('component_log_tail_drawer.status_displaced') }}
        </a-tag>
        <a-tag v-else color="default">
          {{ t('component_log_tail_drawer.status_disconnected') }}
        </a-tag>
        <a-button size="small" @click="clearBuffer">
          {{ t('component_log_tail_drawer.clear') }}
        </a-button>
        <a-button
          v-if="!connected"
          size="small"
          type="primary"
          @click="reconnectManually"
        >
          {{ t('component_log_tail_drawer.reconnect') }}
        </a-button>
      </a-space>
    </template>

    <!-- Soft banner for the "another tab took over" state; users are
         likely to hit this when they refresh the page with the drawer
         already open. -->
    <a-alert
      v-if="displaced"
      :message="t('component_log_tail_drawer.displaced_banner')"
      type="warning"
      show-icon
      :closable="false"
      style="margin: 8px; flex: 0 0 auto"
    />
    <a-alert
      v-else-if="errorMsg"
      :message="errorMsg"
      type="error"
      show-icon
      :closable="false"
      style="margin: 8px; flex: 0 0 auto"
    />

    <div
      ref="scrollEl"
      class="log-tail-scroll"
      @scroll="handleScroll"
    >
      <div
        v-if="lines.length === 0"
        class="log-tail-empty"
      >
        {{ t('component_log_tail_drawer.empty') }}
      </div>
      <div
        v-for="(entry, idx) in lines"
        :key="idx"
        class="log-tail-row"
      >
        <span class="log-tail-ts">{{ entry.ts }}</span>
        <span
          class="log-tail-level"
          :style="{ color: levelColor(entry.level) }"
        >[{{ (entry.level || 'info').toUpperCase() }}]</span>
        <span
          v-if="!props.jobId && entry.jobId"
          class="log-tail-job"
        >{{ entry.jobId }}</span>
        <span
          class="log-tail-line"
          :style="{ color: levelColor(entry.level) }"
        >{{ entry.line }}</span>
      </div>
    </div>

    <div class="log-tail-footer">
      <span>{{ t('component_log_tail_drawer.footer_count', { n: lines.length, max: TAIL_LIMIT }) }}</span>
      <span v-if="!autoScroll" class="log-tail-paused">
        {{ t('component_log_tail_drawer.autoscroll_paused') }}
      </span>
    </div>
  </a-drawer>
</template>

<style scoped>
.log-tail-scroll {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 8px 12px;
  font-family: 'JetBrains Mono', 'Menlo', 'Consolas', monospace;
  font-size: 12px;
  line-height: 1.55;
  color: rgba(255, 255, 255, 0.85);
  background: #1e1e1e;
}

.log-tail-empty {
  color: rgba(255, 255, 255, 0.35);
  text-align: center;
  margin-top: 48px;
}

.log-tail-row {
  white-space: pre-wrap;
  word-break: break-all;
  display: flex;
  gap: 8px;
}

.log-tail-ts {
  color: rgba(255, 255, 255, 0.35);
  flex: 0 0 auto;
  user-select: none;
}

.log-tail-level {
  flex: 0 0 auto;
  font-weight: 600;
}

.log-tail-job {
  color: #91caff;
  flex: 0 0 auto;
}

.log-tail-line {
  flex: 1 1 auto;
}

.log-tail-footer {
  flex: 0 0 auto;
  padding: 6px 12px;
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.45);
  background: #141414;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}

.log-tail-paused {
  color: #faad14;
}
</style>
