<script setup lang="ts">
/**
 * NotificationBell — header badge + drawer for in-app watchdog alerts.
 *
 * Polls /api/notifications?unread_only=true every 30 s while the user is
 * authenticated. Clicking the bell opens a drawer that shows the full list
 * (newest first) with severity colouring, age label, and per-row ack.
 *
 * The component deliberately does NOT use the SSE stream — it keeps its own
 * 30 s poll so it stays decoupled from the events hub and works even if SSE
 * is disabled.
 */
import { ref, computed, onMounted, onUnmounted, watch } from 'vue';
import { BellOutlined, BellFilled } from '@ant-design/icons-vue';
import { useI18n } from 'vue-i18n';
import { useAuthStore } from '../store/auth';
import {
  listNotifications,
  ackNotification,
  markAllRead as apiMarkAllRead,
  type AppNotification,
} from '../api/notifications';
import EmptyState from './EmptyState.vue';

const { t } = useI18n();
const auth = useAuthStore();

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const drawerOpen = ref(false);
const notifications = ref<AppNotification[]>([]);
const loading = ref(false);
const markingAll = ref(false);

const unreadCount = computed(
  () => notifications.value.filter((n) => n.read_at === null).length,
);

// ---------------------------------------------------------------------------
// Severity helpers
// ---------------------------------------------------------------------------

type Severity = 'info' | 'warn' | 'error';

const severityColor: Record<Severity, string> = {
  info: '#1677ff',
  warn: '#fa8c16',
  error: '#f5222d',
};

const severityLabel = (s: Severity): string =>
  t(`component_notification_bell.severity_${s}`);

// ---------------------------------------------------------------------------
// Age formatting (compact relative)
// ---------------------------------------------------------------------------

function formatAge(isoTs: string): string {
  const now = Date.now();
  const then = new Date(isoTs).getTime();
  const diffS = Math.max(0, Math.floor((now - then) / 1000));
  if (diffS < 60) return `${diffS}s`;
  if (diffS < 3600) return `${Math.floor(diffS / 60)}m`;
  if (diffS < 86400) return `${Math.floor(diffS / 3600)}h`;
  return `${Math.floor(diffS / 86400)}d`;
}

// ---------------------------------------------------------------------------
// Fetch
// ---------------------------------------------------------------------------

async function fetchAll(): Promise<void> {
  if (!auth.isAuthenticated) return;
  try {
    notifications.value = await listNotifications({ limit: 50 });
  } catch {
    // Silently swallow — bell should never crash the app.
  }
}

async function fetchUnreadCount(): Promise<void> {
  if (!auth.isAuthenticated) return;
  try {
    const unread = await listNotifications({ unread_only: true, limit: 1 });
    // Update count without replacing the full list (avoids flicker when drawer is open).
    // We do a full refresh only when the drawer is opened.
    if (!drawerOpen.value) {
      // If server reports unread and our local list is stale, re-fetch silently.
      const localUnread = notifications.value.filter((n) => n.read_at === null).length;
      if (unread.length !== localUnread) {
        await fetchAll();
      }
    }
  } catch {
    // swallow
  }
}

// ---------------------------------------------------------------------------
// Polling (30 s)
// ---------------------------------------------------------------------------

let pollTimer: ReturnType<typeof setInterval> | null = null;

function startPolling(): void {
  if (pollTimer !== null) return;
  pollTimer = setInterval(fetchUnreadCount, 30_000);
}

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

onMounted(async () => {
  if (auth.isAuthenticated) {
    await fetchAll();
    startPolling();
  }
});

onUnmounted(() => {
  stopPolling();
});

watch(
  () => auth.isAuthenticated,
  async (authed) => {
    if (authed) {
      await fetchAll();
      startPolling();
    } else {
      notifications.value = [];
      stopPolling();
    }
  },
);

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function openDrawer(): Promise<void> {
  drawerOpen.value = true;
  loading.value = true;
  try {
    await fetchAll();
  } finally {
    loading.value = false;
  }
}

async function handleAck(n: AppNotification): Promise<void> {
  try {
    await ackNotification(n.id);
    n.read_at = new Date().toISOString();
  } catch {
    // swallow
  }
}

async function handleMarkAll(): Promise<void> {
  markingAll.value = true;
  try {
    await apiMarkAllRead();
    const now = new Date().toISOString();
    notifications.value.forEach((n) => {
      if (n.read_at === null) n.read_at = now;
    });
  } catch {
    // swallow
  } finally {
    markingAll.value = false;
  }
}
</script>

<template>
  <!-- Only show the bell when authenticated -->
  <template v-if="auth.isAuthenticated">
    <a-badge :count="unreadCount" :overflow-count="99">
      <a-button
        type="text"
        :style="{
          padding: '0 8px',
          height: '40px',
          color: unreadCount > 0 ? '#fa8c16' : 'inherit',
        }"
        :title="t('component_notification_bell.bell_title')"
        @click="openDrawer"
      >
        <template #icon>
          <BellFilled v-if="unreadCount > 0" />
          <BellOutlined v-else />
        </template>
      </a-button>
    </a-badge>

    <a-drawer
      :open="drawerOpen"
      :title="t('component_notification_bell.drawer_title')"
      :width="400"
      placement="right"
      @close="drawerOpen = false"
    >
      <!-- Header actions -->
      <template #extra>
        <a-button
          v-if="unreadCount > 0"
          size="small"
          type="link"
          :loading="markingAll"
          @click="handleMarkAll"
        >
          {{ t('component_notification_bell.mark_all_read') }}
        </a-button>
      </template>

      <!-- Loading skeleton -->
      <a-skeleton v-if="loading" active :paragraph="{ rows: 4 }" />

      <!-- Empty state (#30 — hint text pulled from /api/meta/hints) -->
      <EmptyState
        v-else-if="notifications.length === 0"
        variant="empty_notifications"
        :title="t('component_notification_bell.empty_state')"
        style="margin-top: 40px"
      />

      <!-- Notification list -->
      <a-list
        v-else
        :data-source="notifications"
        :split="true"
        item-layout="horizontal"
      >
        <template #renderItem="{ item: n }">
          <a-list-item
            :style="{
              background: n.read_at === null ? 'rgba(22,119,255,0.04)' : 'transparent',
              padding: '10px 4px',
              transition: 'background 0.2s',
            }"
          >
            <a-list-item-meta>
              <template #avatar>
                <span
                  :style="{
                    display: 'inline-block',
                    width: '8px',
                    height: '8px',
                    borderRadius: '50%',
                    background: severityColor[(n.severity as 'info'|'warn'|'error')],
                    marginTop: '6px',
                  }"
                />
              </template>
              <template #title>
                <span
                  :style="{
                    fontWeight: n.read_at === null ? '600' : '400',
                    fontSize: '13px',
                  }"
                >
                  {{ n.title }}
                </span>
              </template>
              <template #description>
                <div style="font-size: 12px; color: rgba(0,0,0,0.45)">
                  {{ n.body }}
                </div>
                <div
                  style="
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    margin-top: 4px;
                    font-size: 11px;
                    color: rgba(0,0,0,0.35);
                  "
                >
                  <a-tag
                    :color="severityColor[(n.severity as 'info'|'warn'|'error')]"
                    style="font-size: 10px; padding: 0 4px; line-height: 16px; border-radius: 2px"
                  >
                    {{ severityLabel(n.severity as 'info'|'warn'|'error') }}
                  </a-tag>
                  <span>{{ formatAge(n.created_at) }}</span>
                </div>
              </template>
            </a-list-item-meta>

            <!-- Ack button (only if unread) -->
            <template #actions>
              <a-button
                v-if="n.read_at === null"
                size="small"
                type="text"
                @click.stop="handleAck(n)"
              >
                {{ t('component_notification_bell.ack') }}
              </a-button>
            </template>
          </a-list-item>
        </template>
      </a-list>
    </a-drawer>
  </template>
</template>
