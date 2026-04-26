<script setup lang="ts">
/**
 * "Rerun with overrides" modal (PM roadmap #5).
 *
 * Opens from the BatchDetail header. Lets the user edit a name + a list of
 * dotted-path key/value overrides, previews the would-be-issued command, and
 * posts to ``/api/batches/{id}/rerun``. On success a toast links to the new
 * batch page. The backend does **not** launch training — a reporter-side
 * polling launcher handles that.
 */
import { computed, onUnmounted, ref, watch } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons-vue';
import { getBatch, rerunBatch } from '../api/client';

// Frontend ack-feedback window (#103 v0.1.5). After a successful rerun
// POST we poll the new batch every 5 s for up to 60 s so the user gets
// either a "✅ rerun started on host X" toast (status flipped to
// running by the host agent) or a "⚠️ no agent picked it up" hint
// (timeout — operator must run the command manually). Numbers come
// from the architect's spec; tweak only if the agent's poll cadence
// changes.
const ACK_POLL_INTERVAL_MS = 5_000;
const ACK_POLL_TIMEOUT_MS = 60_000;

const { t } = useI18n();

const props = defineProps<{
  open: boolean;
  batchId: string;
  /** Used to prefill the default "<name> (rerun)" value. */
  sourceName?: string | null;
  /** Displayed in the command preview so users can eyeball the launcher invocation. */
  sourceCommand?: string | null;
}>();

const emit = defineEmits<{
  (e: 'update:open', v: boolean): void;
  /** Fired with the newly-created batch id so the host page can route there. */
  (e: 'rerun-created', newBatchId: string): void;
}>();

interface OverrideRow {
  key: string;
  value: string;
}

const nameInput = ref<string>('');
const overrides = ref<OverrideRow[]>([{ key: '', value: '' }]);
const submitting = ref(false);

// AbortController so the ack-poll loop terminates cleanly if the
// component unmounts mid-flight (e.g. user navigates away from
// BatchDetail before the 60s window finishes). Without this the
// loop's setTimeout chain keeps issuing /api/batches/{id} fetches
// against an unmounted component, leaking a request every 5 s and
// occasionally surfacing a stale-state notification.
let pollAbort: AbortController | null = null;

/** Reset local state every time the modal opens so users don't see stale rows. */
watch(
  () => props.open,
  (o) => {
    if (o) {
      nameInput.value = defaultName();
      overrides.value = [{ key: '', value: '' }];
      submitting.value = false;
    }
  },
);

function defaultName(): string {
  const src = (props.sourceName ?? props.batchId) || props.batchId;
  return `${src} (rerun)`;
}

function addRow(): void {
  overrides.value.push({ key: '', value: '' });
}

function removeRow(idx: number): void {
  overrides.value.splice(idx, 1);
  if (overrides.value.length === 0) overrides.value.push({ key: '', value: '' });
}

/**
 * Coerce a string into a JSON-compatible value.
 *  "256" → 256, "0.001" → 0.001, "true"/"false" → bool, "null" → null,
 *  anything else stays a string. Lets users type Hydra-style CLI values
 *  without worrying about quoting.
 */
function coerce(raw: string): unknown {
  const s = raw.trim();
  if (s === '') return '';
  if (s === 'null') return null;
  if (s === 'true') return true;
  if (s === 'false') return false;
  if (/^-?\d+$/.test(s)) return Number.parseInt(s, 10);
  if (/^-?\d*\.\d+(?:[eE][+-]?\d+)?$/.test(s)) return Number.parseFloat(s);
  return s;
}

/** Map of non-empty overrides, ready for the POST body. */
const overridesMap = computed<Record<string, unknown>>(() => {
  const out: Record<string, unknown> = {};
  for (const row of overrides.value) {
    const k = row.key.trim();
    if (!k) continue;
    out[k] = coerce(row.value);
  }
  return out;
});

/** Preview of the command a launcher would issue (informational only). */
const commandPreview = computed<string>(() => {
  const base = (props.sourceCommand ?? '').trim() || 'main.py';
  const parts: string[] = [base];
  for (const row of overrides.value) {
    const k = row.key.trim();
    if (!k) continue;
    const v = row.value.trim();
    parts.push(`${k}=${v}`);
  }
  return parts.join(' ');
});

const canSubmit = computed<boolean>(() => {
  if (submitting.value) return false;
  // At least one override key OR a custom name to distinguish from the source.
  const nonEmptyKey = overrides.value.some((r) => r.key.trim() !== '');
  const nameChanged = nameInput.value.trim() !== '' && nameInput.value.trim() !== defaultName();
  return nonEmptyKey || nameChanged;
});

/**
 * Poll the new batch every 5 s for up to 60 s, looking for a status
 * flip out of ``requested``. The host agent's ack endpoint is what
 * causes that flip — see ``backend/services/executor.py`` and
 * ``backend/api/agents.py::ack_job``. Three outcomes:
 *
 *   * status flipped (running / done / failed) within window → green toast
 *   * still ``requested`` after timeout → warning toast suggesting manual
 *     ``argus-agent`` start
 *   * fetch errors are swallowed silently (axios interceptor already
 *     surfaced the failure; we don't want to double-notify)
 */
async function pollForAck(
  newBatchId: string,
  displayName: string,
  signal: AbortSignal,
): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < ACK_POLL_TIMEOUT_MS) {
    if (signal.aborted) return;
    await new Promise<void>((resolve) => {
      const handle = setTimeout(resolve, ACK_POLL_INTERVAL_MS);
      // Wake the sleep early when the component unmounts so the loop
      // can exit on the next ``signal.aborted`` check instead of
      // sitting on the full 5 s tick.
      signal.addEventListener(
        'abort',
        () => {
          clearTimeout(handle);
          resolve();
        },
        { once: true },
      );
    });
    if (signal.aborted) return;
    try {
      const batch = await getBatch(newBatchId);
      if (signal.aborted) return;
      if (batch.status && batch.status !== 'requested') {
        notification.success({
          message: t('component_rerun_modal.toast_ack_success'),
          description: t('component_rerun_modal.toast_ack_success_desc', {
            name: displayName,
            host: batch.host ?? '?',
            status: batch.status,
          }),
        });
        return;
      }
    } catch {
      // ignore — fetch failures don't end the wait window
    }
  }
  if (signal.aborted) return;
  notification.warning({
    message: t('component_rerun_modal.toast_ack_timeout'),
    description: t('component_rerun_modal.toast_ack_timeout_desc', {
      name: displayName,
    }),
    duration: 0, // sticky — operator action required
  });
}

onUnmounted(() => {
  // Tear down any in-flight poll when the modal's host component
  // unmounts — usually because the user navigated away from
  // BatchDetail before the 60 s ack window expired.
  if (pollAbort !== null) {
    pollAbort.abort();
    pollAbort = null;
  }
});

async function handleSubmit(): Promise<void> {
  if (submitting.value) return;
  submitting.value = true;
  try {
    const result = await rerunBatch(
      props.batchId,
      overridesMap.value,
      nameInput.value,
    );
    notification.success({
      message: t('component_rerun_modal.toast_success'),
      description: t('component_rerun_modal.toast_success_desc', {
        name: result.name ?? result.batch_id,
      }),
      btn: undefined,
    });
    emit('rerun-created', result.batch_id);
    emit('update:open', false);
    // Fire-and-forget the ack poll. The modal is already closed so
    // there's no UI to block; the toasts surface the outcome.
    // ``pollAbort`` lets ``onUnmounted`` cancel the loop if the user
    // navigates away from BatchDetail mid-window.
    if (pollAbort !== null) pollAbort.abort();
    pollAbort = new AbortController();
    void pollForAck(
      result.batch_id,
      result.name ?? result.batch_id,
      pollAbort.signal,
    );
  } catch {
    // http interceptor already surfaced a notification.
  } finally {
    submitting.value = false;
  }
}

function handleCancel(): void {
  if (submitting.value) return;
  emit('update:open', false);
}
</script>

<template>
  <a-modal
    :open="props.open"
    :title="t('component_rerun_modal.title', { batchId: props.batchId })"
    :ok-text="t('component_rerun_modal.btn_confirm')"
    :cancel-text="t('component_rerun_modal.btn_cancel')"
    :confirm-loading="submitting"
    :ok-button-props="{ disabled: !canSubmit }"
    width="640px"
    @ok="handleSubmit"
    @cancel="handleCancel"
    @update:open="(v: boolean) => emit('update:open', v)"
  >
    <a-form layout="vertical">
      <a-form-item :label="t('component_rerun_modal.label_name')">
        <a-input
          v-model:value="nameInput"
          :placeholder="defaultName()"
          :maxlength="160"
        />
      </a-form-item>

      <a-form-item :label="t('component_rerun_modal.label_overrides')">
        <div style="font-size: 12px; color: #888; margin-bottom: 6px">
          {{ t('component_rerun_modal.overrides_hint') }}
        </div>
        <div
          v-for="(row, idx) in overrides"
          :key="idx"
          style="display: flex; gap: 8px; margin-bottom: 6px; align-items: center"
        >
          <a-input
            v-model:value="row.key"
            :placeholder="t('component_rerun_modal.placeholder_key')"
            style="flex: 1"
          />
          <span style="color: #aaa">=</span>
          <a-input
            v-model:value="row.value"
            :placeholder="t('component_rerun_modal.placeholder_value')"
            style="flex: 1"
          />
          <a-button
            size="small"
            :disabled="overrides.length <= 1 && !row.key && !row.value"
            @click="removeRow(idx)"
          >
            <template #icon><DeleteOutlined /></template>
          </a-button>
        </div>
        <a-button size="small" style="margin-top: 4px" @click="addRow">
          <template #icon><PlusOutlined /></template>
          {{ t('component_rerun_modal.btn_add_row') }}
        </a-button>
      </a-form-item>

      <a-form-item :label="t('component_rerun_modal.label_preview')">
        <a-typography-paragraph
          :copyable="{ text: commandPreview }"
          style="margin-bottom: 0; font-family: monospace; font-size: 12px; background: #f5f5f5; padding: 8px; border-radius: 4px; word-break: break-all"
        >
          {{ commandPreview }}
        </a-typography-paragraph>
        <div style="font-size: 11px; color: #999; margin-top: 4px">
          {{ t('component_rerun_modal.preview_hint') }}
        </div>
      </a-form-item>
    </a-form>
  </a-modal>
</template>
