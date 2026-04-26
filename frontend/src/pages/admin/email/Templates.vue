<script setup lang="ts">
/**
 * /admin/email/templates — list + edit transactional email templates.
 *
 * Monaco is intentionally NOT pulled in — it would triple the bundle for a
 * page admins touch twice a quarter. A plain `<a-textarea>` with
 * monospace + syntax-adjacent Ant affordances is enough.
 */

import { computed, onMounted, reactive, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import {
  ReloadOutlined,
  EditOutlined,
  SaveOutlined,
  EyeOutlined,
  UndoOutlined,
} from '@ant-design/icons-vue';
import {
  listEmailTemplates,
  getEmailTemplate,
  updateEmailTemplate,
  previewEmailTemplate,
  resetEmailTemplate,
  type EmailTemplate,
  type EmailTemplatePreview,
} from '../../../api/email';
import { fmtTime, fmtRelative } from '../../../utils/format';

const { t } = useI18n();

const templates = ref<EmailTemplate[]>([]);
const loading = ref(false);

// Drawer state
const drawerOpen = ref(false);
const editing = ref<EmailTemplate | null>(null);
const saving = ref(false);
const previewing = ref(false);
const resetting = ref(false);
const preview = ref<EmailTemplatePreview | null>(null);
const draft = reactive({
  subject: '',
  body_html: '',
  body_text: '',
});

const columns = computed(() => [
  { title: t('page_admin_email_templates.col_event'), dataIndex: 'event_type', key: 'event_type', width: 200 },
  { title: t('page_admin_email_templates.col_locale'), dataIndex: 'locale', key: 'locale', width: 100 },
  { title: t('page_admin_email_templates.col_subject'), key: 'subject' },
  { title: t('page_admin_email_templates.col_updated'), key: 'updated_at', width: 180 },
  { title: t('page_admin_email_templates.col_actions'), key: 'actions', width: 120, fixed: 'right' as const },
]);

async function fetchAll(): Promise<void> {
  loading.value = true;
  try {
    templates.value = (await listEmailTemplates()) ?? [];
  } catch {
    // interceptor notified
  } finally {
    loading.value = false;
  }
}

async function openEditor(row: EmailTemplate): Promise<void> {
  // Re-fetch the single row so we get body fields if the list endpoint
  // truncates them (common pattern — list returns summary, detail returns body).
  try {
    const full = await getEmailTemplate(row.id);
    editing.value = full;
    draft.subject = full.subject;
    draft.body_html = full.body_html;
    draft.body_text = full.body_text;
    preview.value = null;
    drawerOpen.value = true;
  } catch {
    // interceptor notified
  }
}

function closeEditor(): void {
  drawerOpen.value = false;
  editing.value = null;
  preview.value = null;
}

async function save(): Promise<void> {
  if (!editing.value) return;
  saving.value = true;
  try {
    await updateEmailTemplate(editing.value.id, {
      subject: draft.subject,
      body_html: draft.body_html,
      body_text: draft.body_text,
    });
    notification.success({
      message: t('page_admin_email_templates.saved_toast'),
      duration: 2,
    });
    await fetchAll();
    closeEditor();
  } catch {
    // interceptor notified
  } finally {
    saving.value = false;
  }
}

async function runPreview(): Promise<void> {
  if (!editing.value) return;
  previewing.value = true;
  try {
    preview.value = await previewEmailTemplate(editing.value.id, {
      subject: draft.subject,
      body_html: draft.body_html,
      body_text: draft.body_text,
    });
  } catch {
    // interceptor notified
  } finally {
    previewing.value = false;
  }
}

async function resetToDefault(): Promise<void> {
  if (!editing.value) return;
  resetting.value = true;
  try {
    await resetEmailTemplate(editing.value.id);
    notification.success({
      message: t('page_admin_email_templates.reset_toast'),
      duration: 2,
    });
    const full = await getEmailTemplate(editing.value.id);
    editing.value = full;
    draft.subject = full.subject;
    draft.body_html = full.body_html;
    draft.body_text = full.body_text;
    preview.value = null;
    await fetchAll();
  } catch {
    // interceptor notified
  } finally {
    resetting.value = false;
  }
}

/**
 * Render the preview HTML inside a sandboxed iframe using a data: URL so it
 * can't reach out to the host origin. `allow-same-origin` is NOT included —
 * template HTML is untrusted admin input, so we deny script execution and
 * cookie access.
 */
const previewIframeSrc = computed<string>(() => {
  if (!preview.value?.body_html) return '';
  const html = preview.value.body_html;
  // Using a blob: URL would be cleaner, but data: keeps it synchronous and
  // avoids lifecycle bookkeeping. Size limit is fine (template body < 100 KB).
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
});

function truncate(text: string, n = 60): string {
  if (!text) return '';
  return text.length > n ? text.slice(0, n) + '…' : text;
}

// Render a Jinja-style placeholder in the variables panel without
// embedding ``{{`` inside a Vue interpolation (which the template
// compiler tries to parse as another expression).
function formatVar(name: string): string {
  return '{{ ' + name + ' }}';
}

onMounted(fetchAll);
</script>

<template>
  <div class="page-container" style="max-width: 1200px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ t('page_admin_email_templates.breadcrumb_admin') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('page_admin_email_templates.breadcrumb_email') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ t('page_admin_email_templates.breadcrumb_templates') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="t('page_admin_email_templates.card_title')">
      <template #extra>
        <a-button :loading="loading" @click="fetchAll">
          <template #icon><ReloadOutlined /></template>
          {{ t('page_admin_email_templates.btn_refresh') }}
        </a-button>
      </template>

      <a-alert
        type="info"
        show-icon
        :message="t('page_admin_email_templates.hint')"
        style="margin-bottom: 16px"
      />

      <a-table
        :columns="columns"
        :data-source="templates"
        :loading="loading"
        :pagination="false"
        row-key="id"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'event_type'">
            <code style="font-size: 12px">{{ record.event_type }}</code>
            <a-tag v-if="record.is_system" color="geekblue" style="margin-left: 6px">
              {{ t('page_admin_email_templates.tag_system') }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'locale'">
            <a-tag>{{ record.locale }}</a-tag>
          </template>
          <template v-else-if="column.key === 'subject'">
            <span>{{ truncate(record.subject, 80) }}</span>
          </template>
          <template v-else-if="column.key === 'updated_at'">
            <template v-if="record.updated_at">
              <a-tooltip :title="fmtTime(record.updated_at)">
                <span>{{ fmtRelative(record.updated_at) }}</span>
              </a-tooltip>
            </template>
            <span v-else class="muted">—</span>
          </template>
          <template v-else-if="column.key === 'actions'">
            <a-button size="small" type="link" @click="openEditor(record as EmailTemplate)">
              <template #icon><EditOutlined /></template>
              {{ t('page_admin_email_templates.btn_edit') }}
            </a-button>
          </template>
        </template>
      </a-table>
    </a-card>

    <!-- Editor drawer -->
    <a-drawer
      :open="drawerOpen"
      :title="editing
        ? t('page_admin_email_templates.editor_title', {
            event: editing.event_type,
            locale: editing.locale,
          })
        : ''"
      width="900"
      :destroy-on-close="true"
      @close="closeEditor"
    >
      <template v-if="editing">
        <a-row :gutter="16">
          <a-col :xs="24" :md="16">
            <a-form layout="vertical">
              <a-form-item :label="t('page_admin_email_templates.field_subject')" required>
                <a-input v-model:value="draft.subject" />
              </a-form-item>

              <a-form-item :label="t('page_admin_email_templates.field_body_html')">
                <a-textarea
                  v-model:value="draft.body_html"
                  :rows="12"
                  :style="{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: '12px' }"
                />
                <div class="muted" style="font-size: 11px; margin-top: 4px">
                  {{ t('page_admin_email_templates.body_html_hint') }}
                </div>
              </a-form-item>

              <a-form-item :label="t('page_admin_email_templates.field_body_text')">
                <a-textarea
                  v-model:value="draft.body_text"
                  :rows="8"
                  :style="{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: '12px' }"
                />
                <div class="muted" style="font-size: 11px; margin-top: 4px">
                  {{ t('page_admin_email_templates.body_text_hint') }}
                </div>
              </a-form-item>
            </a-form>

            <a-space wrap>
              <a-button :loading="previewing" @click="runPreview">
                <template #icon><EyeOutlined /></template>
                {{ t('page_admin_email_templates.btn_preview') }}
              </a-button>
              <a-popconfirm
                v-if="editing.is_system"
                :title="t('page_admin_email_templates.reset_confirm')"
                :ok-text="t('page_admin_email_templates.reset_ok')"
                :cancel-text="t('page_admin_email_templates.reset_cancel')"
                @confirm="resetToDefault"
              >
                <a-button :loading="resetting" danger>
                  <template #icon><UndoOutlined /></template>
                  {{ t('page_admin_email_templates.btn_reset') }}
                </a-button>
              </a-popconfirm>
              <a-button type="primary" :loading="saving" @click="save">
                <template #icon><SaveOutlined /></template>
                {{ t('page_admin_email_templates.btn_save') }}
              </a-button>
            </a-space>

            <a-divider />

            <div v-if="preview">
              <h4 style="margin-bottom: 8px">
                {{ t('page_admin_email_templates.preview_heading') }}
              </h4>
              <a-descriptions :column="1" size="small" bordered style="margin-bottom: 12px">
                <a-descriptions-item :label="t('page_admin_email_templates.field_subject')">
                  <code>{{ preview.subject }}</code>
                </a-descriptions-item>
              </a-descriptions>

              <a-tabs>
                <a-tab-pane key="html" :tab="t('page_admin_email_templates.tab_html')">
                  <iframe
                    :src="previewIframeSrc"
                    sandbox=""
                    style="width: 100%; height: 420px; border: 1px solid #444; border-radius: 4px; background: #fff"
                  />
                </a-tab-pane>
                <a-tab-pane key="text" :tab="t('page_admin_email_templates.tab_text')">
                  <pre
                    style="
                      white-space: pre-wrap;
                      background: rgba(0,0,0,0.04);
                      padding: 12px;
                      border-radius: 4px;
                      font-size: 12px;
                      max-height: 420px;
                      overflow: auto;
                    "
                  >{{ preview.body_text }}</pre>
                </a-tab-pane>
              </a-tabs>
            </div>
          </a-col>

          <a-col :xs="24" :md="8">
            <a-card size="small" :title="t('page_admin_email_templates.variables_title')">
              <p class="muted" style="font-size: 12px; margin-bottom: 10px">
                {{ t('page_admin_email_templates.variables_hint') }}
              </p>
              <ul
                v-if="editing.available_variables && editing.available_variables.length > 0"
                style="list-style: none; padding: 0; margin: 0"
              >
                <li
                  v-for="v in editing.available_variables"
                  :key="v"
                  style="margin-bottom: 6px; font-family: ui-monospace, monospace; font-size: 12px"
                >
                  <code>{{ formatVar(v) }}</code>
                </li>
              </ul>
              <p v-else class="muted" style="font-size: 12px; margin: 0">
                {{ t('page_admin_email_templates.variables_empty') }}
              </p>
            </a-card>
          </a-col>
        </a-row>
      </template>
    </a-drawer>
  </div>
</template>
