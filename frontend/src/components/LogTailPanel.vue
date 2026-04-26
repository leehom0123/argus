<script setup lang="ts">
/**
 * LogTailPanel — first-class embedded log tail (#104).
 *
 * Wraps the SSE endpoint ``/api/(batches|jobs)/.../logs/stream`` in an
 * inline panel (no drawer). Replaces LogTailDrawer.vue on JobDetail —
 * the drawer wrapper added cognitive load for users who already wanted
 * logs to be visible by default. The underlying SSE / ring-buffer /
 * autoscroll logic is identical to the old drawer; only the chrome
 * differs (a-card body instead of a-drawer body).
 *
 * Props:
 *   batchId   — required; selects the batch-scoped endpoint by default
 *   jobId     — optional; switches to the job-scoped endpoint
 *   height    — optional CSS height; defaults to fluid (flex: 1)
 *
 * Emits no events — the panel is fully self-contained, including its
 * own filter / level controls.
 *
 * Design notes mirror LogTailDrawer (kept in tree for batch-only views
 * that still want the drawer surface):
 *   - EventSource can't set Authorization headers, so the JWT / API
 *     token is passed via ``?token=`` (same shim as other SSE surfaces).
 *   - Ring buffer capped at TAIL_LIMIT lines — overflow drops oldest.
 *   - Auto-scroll only when the user is already at the bottom.
 *   - ``displaced`` frames surface as a banner instead of silent close.
 */
import { ref, watch, nextTick, onBeforeUnmount, computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';

const { t } = useI18n();

const props = defineProps<{
  batchId: string;
  jobId?: string | null;
  /** CSS height value (e.g. ``'520px'`` or ``'100%'``). Defaults to fluid. */
  height?: string;
}>();

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const TAIL_LIMIT = 5000;
const LS_TOKEN_KEY = 'argus.access_token';

type Level = 'error' | 'warning' | 'info' | 'debug' | string;
interface LogEntry {
  ts: string;
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
const levelFilter = ref<string>('all');
const searchText = ref<string>('');

let source: EventSource | null = null;
let reconnectTimer: number | null = null;
let backoff = 1000;

const filteredLines = computed(() => {
  const lf = levelFilter.value;
  const q = searchText.value.trim().toLowerCase();
  if (lf === 'all' && !q) return lines.value;
  return lines.value.filter((l) => {
    if (lf !== 'all') {
      const lv = (l.level ?? '').toLowerCase();
      if (lf === 'warn' && !(lv === 'warn' || lv === 'warning')) return false;
      if (lf !== 'warn' && lv !== lf) return false;
    }
    if (q && !(l.line ?? '').toLowerCase().includes(q)) return false;
    return true;
  });
});

// ---------------------------------------------------------------------------
// SSE connection lifecycle
// ---------------------------------------------------------------------------

function endpointUrl(token: string): string {
  const tokenQs = `token=${encodeURIComponent(token)}`;
  if (props.jobId) {
    return `/api/jobs/${encodeURIComponent(props.batchId)}/${encodeURIComponent(props.jobId)}/logs/stream?${tokenQs}`;
  }
  return `/api/batches/${encodeURIComponent(props.batchId)}/logs/stream?${tokenQs}`;
}

function teardown(): void {
  if (source) {
    try { source.close(); } catch { /* already closed */ }
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
  const delay = Math.min(30_000, backoff);
  backoff = Math.min(30_000, backoff * 2);
  reconnectTimer = window.setTimeout(connect, delay);
}

function connect(): void {
  teardown();
  if (!props.batchId) return;
  const token = localStorage.getItem(LS_TOKEN_KEY) ?? '';
  if (!token) {
    errorMsg.value = t('component_log_tail_panel.error_no_token');
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

  source.onerror = () => {
    connected.value = false;
    if (!displaced.value) {
      errorMsg.value = t('component_log_tail_panel.error_disconnected');
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

function toggleFollow(): void {
  autoScroll.value = !autoScroll.value;
  if (autoScroll.value) {
    void nextTick(() => {
      const el = scrollEl.value;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }
}

// ---------------------------------------------------------------------------
// Reconnect on identity change. Mounted = first connect.
// ---------------------------------------------------------------------------

watch(
  () => [props.batchId, props.jobId],
  () => {
    lines.value = [];
    connect();
  },
);

onMounted(connect);
onBeforeUnmount(teardown);

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

const panelStyle = computed(() => ({
  height: props.height ?? '100%',
  minHeight: '320px',
  display: 'flex',
  flexDirection: 'column' as const,
}));
</script>

<template>
  <div class="log-tail-panel" :style="panelStyle" data-test="log-tail-panel">
    <div class="log-tail-toolbar">
      <a-tag v-if="connected" color="green" class="log-tail-status">
        {{ t('component_log_tail_panel.status_live') }}
      </a-tag>
      <a-tag v-else-if="displaced" color="orange" class="log-tail-status">
        {{ t('component_log_tail_panel.status_displaced') }}
      </a-tag>
      <a-tag v-else color="default" class="log-tail-status">
        {{ t('component_log_tail_panel.status_disconnected') }}
      </a-tag>

      <a-input-search
        v-model:value="searchText"
        :placeholder="t('component_log_tail_panel.filter_placeholder')"
        allow-clear
        size="small"
        class="log-tail-search"
      />
      <a-select
        v-model:value="levelFilter"
        size="small"
        class="log-tail-level-select"
      >
        <a-select-option value="all">
          {{ t('component_log_tail_panel.level_all') }}
        </a-select-option>
        <a-select-option value="error">ERROR</a-select-option>
        <a-select-option value="warn">WARN</a-select-option>
        <a-select-option value="info">INFO</a-select-option>
        <a-select-option value="debug">DEBUG</a-select-option>
      </a-select>
      <a-button size="small" @click="toggleFollow">
        {{ autoScroll
          ? t('component_log_tail_panel.follow_on')
          : t('component_log_tail_panel.follow_off') }}
      </a-button>
      <a-button size="small" @click="clearBuffer">
        {{ t('component_log_tail_panel.clear') }}
      </a-button>
      <a-button
        v-if="!connected && !displaced"
        size="small"
        type="primary"
        @click="reconnectManually"
      >
        {{ t('component_log_tail_panel.reconnect') }}
      </a-button>
    </div>

    <a-alert
      v-if="displaced"
      :message="t('component_log_tail_panel.displaced_banner')"
      type="warning"
      show-icon
      :closable="false"
      class="log-tail-alert"
    />
    <a-alert
      v-else-if="errorMsg"
      :message="errorMsg"
      type="error"
      show-icon
      :closable="false"
      class="log-tail-alert"
    />

    <div
      ref="scrollEl"
      class="log-tail-scroll"
      @scroll="handleScroll"
    >
      <div v-if="filteredLines.length === 0" class="log-tail-empty">
        {{ t('component_log_tail_panel.empty') }}
      </div>
      <div
        v-for="(entry, idx) in filteredLines"
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
      <span>{{ t('component_log_tail_panel.footer_count', {
        n: filteredLines.length,
        total: lines.length,
        max: TAIL_LIMIT,
      }) }}</span>
      <span v-if="!autoScroll" class="log-tail-paused">
        {{ t('component_log_tail_panel.autoscroll_paused') }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.log-tail-panel {
  background: #1e1e1e;
  border-radius: 6px;
  overflow: hidden;
}

.log-tail-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  background: #141414;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.log-tail-status { font-size: 11px; line-height: 18px; }
.log-tail-search { width: 200px; }
.log-tail-level-select { width: 110px; }

.log-tail-alert {
  margin: 8px;
  flex: 0 0 auto;
}

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
