<script setup lang="ts">
/**
 * /settings/notifications — per-project per-event subscription toggles.
 *
 * UI shape: an event-×-project matrix (events as rows, projects as columns +
 * a "Global default" column). Rows: failed, diverged, completed, stalled, …
 * Clicking a cell toggles `enabled` locally; "Save" posts the delta so the
 * backend can upsert / delete rows atomically.
 */

import { computed, onMounted, reactive, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { ReloadOutlined, SaveOutlined, InfoCircleOutlined } from '@ant-design/icons-vue';
import {
  getMySubscriptions,
  patchSubscriptions,
  type SubscriptionRow,
} from '../../api/email';
import {
  getNotificationPrefs,
  putNotificationPrefs,
  type NotificationPrefs,
} from '../../api/me';
import { listProjects } from '../../api/projects';
import type { ProjectSummary } from '../../types';
import ProjectRecipientsPanel from '../../components/ProjectRecipientsPanel.vue';

const { t } = useI18n();

// ---- Canonical event catalog ---------------------------------------------
// We don't fetch this from the backend — the list is stable and the UI
// needs labels anyway. Backend-side any unknown event_type a subscription
// row references will still render as an extra row (see `mergedEvents`).
interface EventDef {
  event_type: string;
  severity: 'critical' | 'warn' | 'info';
  /** Whether the backend turns this on by default for new users. */
  default_on: boolean;
}

const CATALOG: EventDef[] = [
  { event_type: 'batch_failed', severity: 'critical', default_on: true },
  { event_type: 'batch_diverged', severity: 'critical', default_on: true },
  { event_type: 'batch_stalled', severity: 'warn', default_on: true },
  { event_type: 'batch_completed', severity: 'info', default_on: false },
  { event_type: 'job_failed', severity: 'critical', default_on: true },
  { event_type: 'job_completed', severity: 'info', default_on: false },
  { event_type: 'host_offline', severity: 'warn', default_on: true },
];

// ---- Reactive state ------------------------------------------------------
const loading = ref(false);
const saving = ref(false);
const projects = ref<string[]>([]);

// ---- Per-user "Email preferences" defaults (#108) ------------------------
// These are the five toggles the user can flip from this page; they act
// as defaults for NEW batches. Per-batch overrides on
// ``batch_email_subscription`` always shadow these at dispatch time —
// the help text below the section spells this out so users don't expect
// flipping a toggle here to retroactively silence a running batch.
const PREF_KEYS: Array<keyof NotificationPrefs> = [
  'notify_batch_done',
  'notify_batch_failed',
  'notify_job_failed',
  'notify_diverged',
  'notify_job_idle',
];
// ``prefs`` mirrors server truth (used to compute the dirty flag);
// ``prefsDraft`` is the editable copy. Both default to all-true so the
// UI doesn't flash an empty state during the initial fetch — the GET
// response will overwrite them anyway.
const prefs = reactive<NotificationPrefs>({
  notify_batch_done: true,
  notify_batch_failed: true,
  notify_job_failed: true,
  notify_diverged: true,
  notify_job_idle: false,
});
const prefsDraft = reactive<NotificationPrefs>({ ...prefs });
const prefsLoading = ref(false);
const prefsSaving = ref(false);

const prefsDirty = computed<boolean>(() => {
  for (const k of PREF_KEYS) {
    if (prefs[k] !== prefsDraft[k]) return true;
  }
  return false;
});

async function loadPrefs(): Promise<void> {
  prefsLoading.value = true;
  try {
    const got = await getNotificationPrefs();
    for (const k of PREF_KEYS) {
      prefs[k] = got[k];
      prefsDraft[k] = got[k];
    }
  } catch {
    // axios interceptor already surfaces a toast; leaving defaults in
    // place is the safest fallback (users can still see something).
  } finally {
    prefsLoading.value = false;
  }
}

async function savePrefs(): Promise<void> {
  prefsSaving.value = true;
  try {
    const updated = await putNotificationPrefs({ ...prefsDraft });
    for (const k of PREF_KEYS) {
      prefs[k] = updated[k];
      prefsDraft[k] = updated[k];
    }
    notification.success({
      message: t('page_settings_notifications.prefs_saved_toast'),
      duration: 2,
    });
  } catch {
    // interceptor notified
  } finally {
    prefsSaving.value = false;
  }
}

// Per-project recipients section: which project the picker is on. The
// caller's "mine" projects are owned, so editing is allowed; for shared
// projects we'd need an explicit ownership query, deferred to a follow-up.
const recipientsProject = ref<string>('');

/**
 * The matrix uses a sparse Map keyed by `"<event>||<project-or-empty>"`. The
 * baseline (server-side truth) sits in `baseline`; the current UI state sits
 * in `current`. "Dirty" cells are computed by diffing the two; only those
 * are sent in PATCH.
 */
const baseline = reactive<Record<string, boolean>>({});
const current = reactive<Record<string, boolean>>({});

function cellKey(eventType: string, project: string | null): string {
  return `${eventType}||${project ?? ''}`;
}

function isOn(eventType: string, project: string | null): boolean {
  const key = cellKey(eventType, project);
  if (key in current) return current[key];
  // No row → fall back to the default from CATALOG (global column) or the
  // global default row (project column with no specific row).
  if (project === null) {
    return CATALOG.find((e) => e.event_type === eventType)?.default_on ?? false;
  }
  // Per-project cell with no explicit row: inherit from global default.
  return isOn(eventType, null);
}

function setCell(eventType: string, project: string | null, enabled: boolean): void {
  current[cellKey(eventType, project)] = enabled;
}

/**
 * Template-friendly wrapper around `setCell` that unwraps the Ant Design
 * CheckboxChangeEvent shape (`e.target.checked`). Writing the type as an
 * inline arrow annotation in the template doesn't lex cleanly, so we route
 * through a method instead.
 */
function onCellChange(
  eventType: string,
  project: string | null,
  e: unknown,
): void {
  // The Ant Design `change` event for checkboxes carries the new state on
  // ``e.target.checked``. We accept ``unknown`` here so the inline template
  // arrow `(e) => onCellChange(...)` doesn't need an explicit type cast
  // (Vue's template compiler can't parse TS `as` expressions).
  const checked = Boolean(
    (e as { target?: { checked?: boolean } } | undefined)?.target?.checked,
  );
  setCell(eventType, project, checked);
}

const mergedEvents = computed<EventDef[]>(() => {
  // Merge catalog + any event_type we got back that isn't in the catalog.
  const seen = new Set(CATALOG.map((e) => e.event_type));
  const extras: EventDef[] = [];
  for (const key of Object.keys(baseline)) {
    const [ev] = key.split('||');
    if (!seen.has(ev)) {
      seen.add(ev);
      extras.push({ event_type: ev, severity: 'info', default_on: false });
    }
  }
  return [...CATALOG, ...extras];
});

const isDirty = computed<boolean>(() => {
  for (const key of Object.keys(current)) {
    const b = baseline[key];
    if (b === undefined || b !== current[key]) return true;
  }
  return false;
});

async function load(): Promise<void> {
  loading.value = true;
  try {
    const [subs, projs] = await Promise.all([
      getMySubscriptions(),
      listProjects({ scope: 'mine' }).catch(() => [] as ProjectSummary[]),
    ]);
    // Reset baseline and current to match server truth.
    for (const k of Object.keys(baseline)) delete baseline[k];
    for (const k of Object.keys(current)) delete current[k];
    for (const s of subs) {
      const k = cellKey(s.event_type, s.project);
      baseline[k] = s.enabled;
      current[k] = s.enabled;
    }
    projects.value = projs.map((p) => p.project).sort();
    if (!recipientsProject.value && projects.value.length > 0) {
      recipientsProject.value = projects.value[0];
    }
  } catch {
    // interceptor notified
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  saving.value = true;
  try {
    const delta: SubscriptionRow[] = [];
    for (const [key, val] of Object.entries(current)) {
      if (baseline[key] === val) continue;
      const [eventType, projectRaw] = key.split('||');
      delta.push({
        event_type: eventType,
        project: projectRaw === '' ? null : projectRaw,
        enabled: val,
      });
    }
    await patchSubscriptions(delta);
    notification.success({
      message: t('page_settings_notifications.saved_toast'),
      duration: 2,
    });
    // Fold current into baseline so isDirty returns false.
    for (const [key, val] of Object.entries(current)) {
      baseline[key] = val;
    }
  } catch {
    // interceptor notified
  } finally {
    saving.value = false;
  }
}

function severityColor(s: EventDef['severity']): string {
  if (s === 'critical') return 'red';
  if (s === 'warn') return 'orange';
  return 'blue';
}

function eventLabel(eventType: string): string {
  // Try locale first; fall back to humanised event type if no locale key.
  const key = `page_settings_notifications.event.${eventType}`;
  const translated = t(key);
  if (translated && translated !== key) return translated;
  return eventType.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

onMounted(() => {
  void load();
  void loadPrefs();
});
</script>

<template>
  <div class="page-container" style="max-width: 1000px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ t('page_settings_notifications.breadcrumb_settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('page_settings_notifications.breadcrumb_notifications') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <!--
      Per-user email-notification defaults (#108). These five toggles
      seed every NEW batch's default subscription; per-batch overrides
      on the BatchDetail "Email notifications" panel always win at
      dispatch time. The hint underneath spells that out so users don't
      expect this card to retroactively silence a running batch.
    -->
    <a-card
      :title="t('page_settings_notifications.prefs_card_title')"
      :loading="prefsLoading"
      style="margin-bottom: 16px"
      data-testid="email-prefs-card"
    >
      <a-alert
        type="info"
        show-icon
        :message="t('page_settings_notifications.prefs_hint')"
        style="margin-bottom: 16px"
      />
      <a-space direction="vertical" :size="8" style="width: 100%">
        <a-checkbox
          v-model:checked="prefsDraft.notify_batch_done"
          data-testid="pref-notify_batch_done"
        >
          {{ t('page_settings_notifications.pref_notify_batch_done') }}
        </a-checkbox>
        <a-checkbox
          v-model:checked="prefsDraft.notify_batch_failed"
          data-testid="pref-notify_batch_failed"
        >
          {{ t('page_settings_notifications.pref_notify_batch_failed') }}
        </a-checkbox>
        <a-checkbox
          v-model:checked="prefsDraft.notify_job_failed"
          data-testid="pref-notify_job_failed"
        >
          {{ t('page_settings_notifications.pref_notify_job_failed') }}
        </a-checkbox>
        <a-checkbox
          v-model:checked="prefsDraft.notify_diverged"
          data-testid="pref-notify_diverged"
        >
          {{ t('page_settings_notifications.pref_notify_diverged') }}
        </a-checkbox>
        <a-checkbox
          v-model:checked="prefsDraft.notify_job_idle"
          data-testid="pref-notify_job_idle"
        >
          {{ t('page_settings_notifications.pref_notify_job_idle') }}
        </a-checkbox>
      </a-space>
      <a-divider style="margin: 16px 0 12px" />
      <a-space>
        <a-button
          type="primary"
          :loading="prefsSaving"
          :disabled="!prefsDirty"
          data-testid="prefs-save-btn"
          @click="savePrefs"
        >
          <template #icon><SaveOutlined /></template>
          {{ t('page_settings_notifications.btn_save') }}
        </a-button>
        <span v-if="!prefsDirty" class="muted" style="font-size: 12px">
          {{ t('page_settings_notifications.no_changes') }}
        </span>
      </a-space>
    </a-card>

    <a-card :title="t('page_settings_notifications.card_title')" :loading="loading">
      <template #extra>
        <a-button :loading="loading" @click="load">
          <template #icon><ReloadOutlined /></template>
          {{ t('page_settings_notifications.btn_refresh') }}
        </a-button>
      </template>

      <a-alert
        type="info"
        show-icon
        :message="t('page_settings_notifications.banner_title')"
        :description="t('page_settings_notifications.banner_desc')"
        style="margin-bottom: 16px"
      >
        <template #icon><InfoCircleOutlined /></template>
      </a-alert>

      <div style="overflow-x: auto">
        <table class="subs-matrix">
          <thead>
            <tr>
              <th class="event-col">{{ t('page_settings_notifications.col_event') }}</th>
              <th class="global-col">{{ t('page_settings_notifications.col_global') }}</th>
              <th
                v-for="p in projects"
                :key="p"
                class="proj-col"
                :title="p"
              >
                {{ p }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="ev in mergedEvents" :key="ev.event_type">
              <td class="event-col">
                <a-tag :color="severityColor(ev.severity)" style="margin-right: 6px">
                  {{ t(`page_settings_notifications.severity_${ev.severity}`) }}
                </a-tag>
                <span>{{ eventLabel(ev.event_type) }}</span>
                <div class="muted" style="font-size: 11px; font-family: ui-monospace, monospace">
                  {{ ev.event_type }}
                </div>
              </td>
              <td class="cell">
                <a-checkbox
                  :checked="isOn(ev.event_type, null)"
                  @change="(e: any) => onCellChange(ev.event_type, null, e)"
                />
              </td>
              <td v-for="p in projects" :key="p" class="cell">
                <a-checkbox
                  :checked="isOn(ev.event_type, p)"
                  @change="(e: any) => onCellChange(ev.event_type, p, e)"
                />
              </td>
            </tr>
            <tr v-if="mergedEvents.length === 0">
              <td :colspan="projects.length + 2" class="muted" style="text-align: center; padding: 16px">
                {{ t('page_settings_notifications.empty_events') }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <a-divider style="margin: 16px 0 12px" />

      <a-space>
        <a-button
          type="primary"
          :loading="saving"
          :disabled="!isDirty"
          @click="save"
        >
          <template #icon><SaveOutlined /></template>
          {{ t('page_settings_notifications.btn_save') }}
        </a-button>
        <span v-if="!isDirty" class="muted" style="font-size: 12px">
          {{ t('page_settings_notifications.no_changes') }}
        </span>
      </a-space>
    </a-card>

    <!-- Per-project multi-recipient list (v0.1.4). Owned projects are
         editable here; shared projects show a read-only widget on the
         ProjectDetail Notifications tab. -->
    <a-card
      :title="t('notifications.recipients.section_title')"
      style="margin-top: 16px"
    >
      <a-space style="margin-bottom: 12px" :size="8">
        <span>{{ t('notifications.recipients.project_picker') }}</span>
        <a-select
          v-model:value="recipientsProject"
          style="min-width: 240px"
          :placeholder="t('notifications.recipients.project_placeholder')"
          :disabled="projects.length === 0"
        >
          <a-select-option v-for="p in projects" :key="p" :value="p">
            {{ p }}
          </a-select-option>
        </a-select>
      </a-space>
      <ProjectRecipientsPanel
        v-if="recipientsProject"
        :project="recipientsProject"
        :can-edit="true"
      />
      <div v-else class="muted" style="text-align: center; padding: 12px">
        {{ t('notifications.recipients.no_projects_hint') }}
      </div>
    </a-card>
  </div>
</template>

<style scoped>
.subs-matrix {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
}
.subs-matrix th,
.subs-matrix td {
  border-bottom: 1px solid rgba(128, 128, 128, 0.2);
  padding: 8px 10px;
  text-align: left;
  vertical-align: middle;
}
.subs-matrix th {
  font-weight: 600;
  background: rgba(128, 128, 128, 0.06);
  white-space: nowrap;
}
.subs-matrix .event-col {
  min-width: 240px;
  max-width: 320px;
}
.subs-matrix .global-col,
.subs-matrix .proj-col {
  text-align: center;
  min-width: 88px;
}
.subs-matrix .cell {
  text-align: center;
}
.subs-matrix .muted {
  opacity: 0.65;
}
</style>
