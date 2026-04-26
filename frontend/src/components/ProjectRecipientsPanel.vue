<script setup lang="ts">
/**
 * ProjectRecipientsPanel — multi-recipient notification list for one project.
 *
 * v0.1.4 ships this widget in two surfaces:
 *
 * * ``/settings/notifications`` (per-project section, project picker is in
 *   the parent so the same widget renders for whichever project the user
 *   selected).
 * * ``/projects/{project}`` Notifications tab (project is fixed by the URL).
 *
 * Behaviour:
 *
 * * Owner / admin sees an editable table — can add, edit, delete rows.
 * * Project-share viewer sees a read-only table (the parent passes
 *   ``can-edit="false"``).
 * * If the list is empty AND the caller is the owner, the "Add recipient"
 *   button pre-fills with the auth store's current-user email + every
 *   event_kind checked, so the most common path (owner subscribes to their
 *   own batches) is one click.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { notification, Modal } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons-vue';
import {
  listProjectRecipients,
  addProjectRecipient,
  updateProjectRecipient,
  deleteProjectRecipient,
  type ProjectRecipient,
} from '../api/email';
import { useAuthStore } from '../store/auth';

const props = defineProps<{
  project: string;
  /** False renders read-only (project-share-viewer mode). */
  canEdit?: boolean;
}>();

const { t } = useI18n();
const auth = useAuthStore();
const myEmail = computed<string>(() => auth.currentUser?.email ?? '');

// Canonical event kinds; mirrors the backend's SUPPORTED_EVENTS subset that
// makes sense for a project-level recipient. ``share_granted`` is a per-user
// event so it's intentionally omitted — recipient lists target project events.
const EVENT_KINDS = [
  'batch_done',
  'batch_failed',
  'batch_diverged',
  'job_failed',
  'job_idle_flagged',
] as const;
type EventKind = (typeof EVENT_KINDS)[number];

const recipients = ref<ProjectRecipient[]>([]);
const loading = ref(false);
const saving = ref(false);

// Add / edit modal state. ``editingId`` distinguishes create vs update.
const modalOpen = ref(false);
const editingId = ref<number | null>(null);
const form = reactive({
  email: '',
  event_kinds: [] as EventKind[],
  enabled: true,
});

function resetForm(): void {
  form.email = '';
  form.event_kinds = [...EVENT_KINDS];
  form.enabled = true;
  editingId.value = null;
}

async function load(): Promise<void> {
  if (!props.project) return;
  loading.value = true;
  try {
    recipients.value = await listProjectRecipients(props.project);
  } catch {
    // axios interceptor already pops a notification
  } finally {
    loading.value = false;
  }
}

function openAdd(): void {
  resetForm();
  // Pre-fill with current user's email when the list is empty + caller can
  // edit; matches the spec's "Default = current user's email at first config"
  // requirement so the owner can confirm with one click.
  if (recipients.value.length === 0 && props.canEdit && myEmail.value) {
    form.email = myEmail.value;
  }
  modalOpen.value = true;
}

function openEdit(row: ProjectRecipient): void {
  editingId.value = row.id;
  form.email = row.email;
  form.event_kinds = row.event_kinds.filter((k): k is EventKind =>
    (EVENT_KINDS as readonly string[]).includes(k),
  );
  form.enabled = row.enabled;
  modalOpen.value = true;
}

async function submit(): Promise<void> {
  if (!form.email) return;
  saving.value = true;
  try {
    if (editingId.value === null) {
      await addProjectRecipient(props.project, {
        email: form.email,
        event_kinds: [...form.event_kinds],
        enabled: form.enabled,
      });
      notification.success({
        message: t('notifications.recipients.added_toast'),
        duration: 2,
      });
    } else {
      await updateProjectRecipient(props.project, editingId.value, {
        email: form.email,
        event_kinds: [...form.event_kinds],
        enabled: form.enabled,
      });
      notification.success({
        message: t('notifications.recipients.updated_toast'),
        duration: 2,
      });
    }
    modalOpen.value = false;
    await load();
  } catch {
    // interceptor notifies; keep the modal open so the user can correct.
  } finally {
    saving.value = false;
  }
}

async function removeRow(row: ProjectRecipient): Promise<void> {
  Modal.confirm({
    title: t('notifications.recipients.delete_confirm_title'),
    content: t('notifications.recipients.delete_confirm_body', { email: row.email }),
    okText: t('notifications.recipients.delete_confirm_ok'),
    okType: 'danger',
    cancelText: t('notifications.recipients.delete_confirm_cancel'),
    onOk: async () => {
      try {
        await deleteProjectRecipient(props.project, row.id);
        notification.success({
          message: t('notifications.recipients.deleted_toast'),
          duration: 2,
        });
        await load();
      } catch {
        // interceptor notifies
      }
    },
  });
}

function hasKind(row: ProjectRecipient, kind: EventKind): boolean {
  return row.event_kinds.includes(kind);
}

function eventLabel(kind: string): string {
  const key = `notifications.recipients.kind_${kind}`;
  const txt = t(key);
  return txt && txt !== key
    ? txt
    : kind.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

watch(() => props.project, () => void load(), { immediate: false });
onMounted(load);
</script>

<template>
  <div class="recipients-panel">
    <div class="header-row">
      <span class="title">
        {{ t('notifications.recipients.title') }}
        <span class="count">({{ recipients.length }})</span>
      </span>
      <a-button
        v-if="canEdit"
        type="primary"
        size="small"
        :loading="loading"
        @click="openAdd"
      >
        <template #icon><PlusOutlined /></template>
        {{ t('notifications.recipients.add_button') }}
      </a-button>
    </div>

    <p v-if="canEdit && recipients.length === 0 && myEmail" class="muted hint">
      {{ t('notifications.recipients.default_hint', { email: myEmail }) }}
    </p>

    <a-table
      :columns="[
        { title: t('notifications.recipients.col_email'), key: 'email', dataIndex: 'email' },
        { title: eventLabel('batch_done'), key: 'batch_done', align: 'center' as const, width: 100 },
        { title: eventLabel('batch_failed'), key: 'batch_failed', align: 'center' as const, width: 100 },
        { title: eventLabel('batch_diverged'), key: 'batch_diverged', align: 'center' as const, width: 110 },
        { title: eventLabel('job_failed'), key: 'job_failed', align: 'center' as const, width: 100 },
        { title: eventLabel('job_idle_flagged'), key: 'job_idle_flagged', align: 'center' as const, width: 110 },
        { title: t('notifications.recipients.col_enabled'), key: 'enabled', align: 'center' as const, width: 90 },
        ...(canEdit
          ? [{ title: t('notifications.recipients.col_actions'), key: 'actions', align: 'right' as const, width: 100 }]
          : []),
      ]"
      :data-source="recipients"
      row-key="id"
      size="small"
      :loading="loading"
      :pagination="false"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'batch_done'">
          <a-checkbox :checked="hasKind(record as ProjectRecipient, 'batch_done')" disabled />
        </template>
        <template v-else-if="column.key === 'batch_failed'">
          <a-checkbox :checked="hasKind(record as ProjectRecipient, 'batch_failed')" disabled />
        </template>
        <template v-else-if="column.key === 'batch_diverged'">
          <a-checkbox :checked="hasKind(record as ProjectRecipient, 'batch_diverged')" disabled />
        </template>
        <template v-else-if="column.key === 'job_failed'">
          <a-checkbox :checked="hasKind(record as ProjectRecipient, 'job_failed')" disabled />
        </template>
        <template v-else-if="column.key === 'job_idle_flagged'">
          <a-checkbox :checked="hasKind(record as ProjectRecipient, 'job_idle_flagged')" disabled />
        </template>
        <template v-else-if="column.key === 'enabled'">
          <a-tag :color="(record as ProjectRecipient).enabled ? 'green' : 'default'">
            {{
              (record as ProjectRecipient).enabled
                ? t('notifications.recipients.tag_on')
                : t('notifications.recipients.tag_off')
            }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'actions' && canEdit">
          <a-space :size="4">
            <a-button size="small" @click="openEdit(record as ProjectRecipient)">
              <template #icon><EditOutlined /></template>
            </a-button>
            <a-button size="small" danger @click="removeRow(record as ProjectRecipient)">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </a-space>
        </template>
      </template>
      <template #emptyText>
        <div class="muted empty-msg">
          {{ canEdit ? t('notifications.recipients.empty_owner') : t('notifications.recipients.empty_viewer') }}
        </div>
      </template>
    </a-table>

    <p class="muted footnote">
      {{ t('notifications.recipients.unsubscribe_self') }}
    </p>

    <a-modal
      v-model:open="modalOpen"
      :title="
        editingId === null
          ? t('notifications.recipients.modal_add_title')
          : t('notifications.recipients.modal_edit_title')
      "
      :ok-text="t('notifications.recipients.modal_save')"
      :cancel-text="t('notifications.recipients.modal_cancel')"
      :confirm-loading="saving"
      :ok-button-props="{ disabled: !form.email || form.event_kinds.length === 0 }"
      :get-container="() => document.body"
      @ok="submit"
    >
      <a-form layout="vertical">
        <a-form-item :label="t('notifications.recipients.col_email')" required>
          <a-input
            v-model:value="form.email"
            :placeholder="t('notifications.recipients.email_placeholder')"
            autocomplete="email"
            autofocus
          />
        </a-form-item>
        <a-form-item :label="t('notifications.recipients.event_kinds_label')" required>
          <a-checkbox-group v-model:value="form.event_kinds">
            <div v-for="k in EVENT_KINDS" :key="k" style="margin-bottom: 4px">
              <a-checkbox :value="k">
                {{ eventLabel(k) }}
              </a-checkbox>
            </div>
          </a-checkbox-group>
        </a-form-item>
        <a-form-item>
          <a-checkbox v-model:checked="form.enabled">
            {{ t('notifications.recipients.enabled_label') }}
          </a-checkbox>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.recipients-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.title {
  font-weight: 600;
}
.count {
  color: rgba(128, 128, 128, 0.7);
  font-weight: 400;
  margin-left: 4px;
}
.hint,
.footnote {
  font-size: 12px;
  margin: 0;
}
.muted {
  color: rgba(128, 128, 128, 0.85);
}
.empty-msg {
  padding: 16px;
  text-align: center;
}
</style>
