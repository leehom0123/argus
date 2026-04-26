<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { notification } from 'ant-design-vue';
import { useI18n } from 'vue-i18n';
import { CopyOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons-vue';
import dayjs from 'dayjs';
import { createToken, listTokens, revokeToken } from '../../api/tokens';
import type { ApiToken, TokenCreateResponse, TokenScope } from '../../types';
import { fmtTime, fmtRelative } from '../../utils/format';

const { t } = useI18n();

// ---- State ----
const tokens = ref<ApiToken[]>([]);
const loading = ref(false);

// "New token" form modal
const createOpen = ref(false);
const creating = ref(false);
const form = ref<{
  name: string;
  scope: TokenScope;
  expiresChoice: 'never' | '30d' | '90d' | '1y';
}>({
  name: '',
  scope: 'reporter',
  expiresChoice: '90d',
});

// "Plaintext shown once" modal
const plaintextOpen = ref(false);
const plaintextToken = ref<TokenCreateResponse | null>(null);

const columns = computed(() => [
  { title: t('page_settings_tokens.col_name'), dataIndex: 'name', key: 'name', width: 180 },
  { title: t('page_settings_tokens.col_token'), key: 'token', width: 220 },
  { title: t('page_settings_tokens.col_scope'), key: 'scope', width: 110 },
  { title: t('page_settings_tokens.col_created'), key: 'created_at', width: 200 },
  { title: t('page_settings_tokens.col_last_used'), key: 'last_used', width: 180 },
  { title: t('page_settings_tokens.col_expires'), key: 'expires_at', width: 180 },
  { title: t('page_settings_tokens.col_actions'), key: 'actions', width: 120, fixed: 'right' as const },
]);

async function fetchAll(): Promise<void> {
  loading.value = true;
  try {
    tokens.value = (await listTokens()) ?? [];
  } catch {
    // interceptor notifies
  } finally {
    loading.value = false;
  }
}

function openCreate(): void {
  form.value = { name: '', scope: 'reporter', expiresChoice: '90d' };
  createOpen.value = true;
}

function expiresAtISO(choice: typeof form.value.expiresChoice): string | null {
  if (choice === 'never') return null;
  const now = dayjs();
  switch (choice) {
    case '30d':
      return now.add(30, 'day').toISOString();
    case '90d':
      return now.add(90, 'day').toISOString();
    case '1y':
      return now.add(1, 'year').toISOString();
    default:
      return null;
  }
}

async function submitCreate(): Promise<void> {
  const name = form.value.name.trim();
  if (!name) {
    notification.warning({ message: t('page_settings_tokens.name_required'), duration: 2 });
    return;
  }
  creating.value = true;
  try {
    const resp = await createToken({
      name,
      scope: form.value.scope,
      expires_at: expiresAtISO(form.value.expiresChoice),
    });
    createOpen.value = false;
    plaintextToken.value = resp;
    plaintextOpen.value = true;
    void fetchAll();
  } catch {
    // interceptor notified
  } finally {
    creating.value = false;
  }
}

async function copyPlaintext(): Promise<void> {
  const tok = plaintextToken.value?.token;
  if (!tok) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(tok);
    } else {
      const ta = document.createElement('textarea');
      ta.value = tok;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    notification.success({ message: t('page_settings_tokens.token_copied'), duration: 2 });
  } catch {
    notification.error({
      message: t('page_settings_tokens.copy_failed'),
      description: t('page_settings_tokens.copy_failed_desc'),
      duration: 4,
    });
  }
}

function closePlaintextModal(): void {
  plaintextOpen.value = false;
  plaintextToken.value = null;
}

async function doRevoke(tok: ApiToken): Promise<void> {
  try {
    await revokeToken(tok.id);
    notification.success({ message: t('page_settings_tokens.revoked_msg'), duration: 2 });
    await fetchAll();
  } catch {
    // interceptor notified
  }
}

function scopeColor(scope: TokenScope): string {
  return scope === 'reporter' ? 'blue' : 'purple';
}

function maskedToken(tok: ApiToken): string {
  const hint = tok.display_hint ?? '';
  return `${tok.prefix}${hint}${hint ? '…' : '••••'}`;
}

const hasTokens = computed(() => tokens.value.length > 0);

onMounted(fetchAll);
</script>

<template>
  <div class="page-container" style="max-width: 960px">
    <a-breadcrumb style="margin-bottom: 12px">
      <a-breadcrumb-item>{{ $t('page_settings_tokens.breadcrumb_settings') }}</a-breadcrumb-item>
      <a-breadcrumb-item>{{ $t('page_settings_tokens.breadcrumb_tokens') }}</a-breadcrumb-item>
    </a-breadcrumb>

    <a-card :title="$t('page_settings_tokens.card_title')">
      <template #extra>
        <a-space>
          <a-button :loading="loading" @click="fetchAll">
            <template #icon><ReloadOutlined /></template>
            {{ $t('page_settings_tokens.btn_refresh') }}
          </a-button>
          <a-button type="primary" @click="openCreate">
            <template #icon><PlusOutlined /></template>
            {{ $t('page_settings_tokens.btn_new_token') }}
          </a-button>
        </a-space>
      </template>

      <a-alert
        type="info"
        show-icon
        :message="$t('page_settings_tokens.alert_info')"
        :description="$t('page_settings_tokens.alert_desc')"
        style="margin-bottom: 16px"
      />

      <a-table
        :columns="columns"
        :data-source="tokens"
        :loading="loading"
        row-key="id"
        size="small"
        :scroll="{ x: 900 }"
        :pagination="false"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'token'">
            <code style="font-size: 12px">{{ maskedToken(record as ApiToken) }}</code>
          </template>
          <template v-else-if="column.key === 'scope'">
            <a-tag :color="scopeColor((record as ApiToken).scope)">
              {{ (record as ApiToken).scope }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'created_at'">
            <div style="line-height: 1.2">
              <div>{{ fmtTime((record as ApiToken).created_at) }}</div>
              <div class="muted" style="font-size: 11px">
                {{ fmtRelative((record as ApiToken).created_at) }}
              </div>
            </div>
          </template>
          <template v-else-if="column.key === 'last_used'">
            <span v-if="(record as ApiToken).last_used">
              {{ fmtRelative((record as ApiToken).last_used) }}
            </span>
            <span v-else class="muted">{{ $t('page_settings_tokens.never_used') }}</span>
          </template>
          <template v-else-if="column.key === 'expires_at'">
            <span v-if="(record as ApiToken).expires_at">
              {{ fmtTime((record as ApiToken).expires_at) }}
            </span>
            <a-tag v-else color="default">{{ $t('page_settings_tokens.never_expires') }}</a-tag>
          </template>
          <template v-else-if="column.key === 'actions'">
            <a-popconfirm
              :title="$t('page_settings_tokens.popconfirm_revoke')"
              :ok-text="$t('page_settings_tokens.btn_revoke')"
              :cancel-text="$t('common.cancel')"
              ok-type="danger"
              @confirm="doRevoke(record as ApiToken)"
            >
              <a-button size="small" danger>{{ $t('page_settings_tokens.btn_revoke') }}</a-button>
            </a-popconfirm>
          </template>
        </template>

        <template #emptyText>
          <div v-if="!loading" class="empty-wrap">
            <div style="font-size: 15px; margin-bottom: 6px">{{ $t('page_settings_tokens.empty_title') }}</div>
            <div class="muted" style="max-width: 520px; margin: 0 auto">
              {{ $t('page_settings_tokens.empty_hint') }}
            </div>
            <a-button type="primary" style="margin-top: 12px" @click="openCreate">
              <template #icon><PlusOutlined /></template>
              {{ $t('page_settings_tokens.btn_create_first') }}
            </a-button>
          </div>
        </template>
      </a-table>

      <div v-if="hasTokens" class="muted" style="margin-top: 10px; font-size: 12px">
        {{ $t('page_settings_tokens.showing_count', { count: tokens.length }) }}
      </div>
    </a-card>

    <!-- Create modal -->
    <a-modal
      v-model:open="createOpen"
      :title="$t('page_settings_tokens.modal_create_title')"
      :confirm-loading="creating"
      :ok-button-props="{ disabled: !form.name.trim() }"
      :ok-text="$t('page_settings_tokens.modal_create_ok')"
      @ok="submitCreate"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('page_settings_tokens.label_name')" required>
          <a-input
            v-model:value="form.name"
            :placeholder="$t('page_settings_tokens.placeholder_name')"
            :maxlength="64"
            allow-clear
          />
          <div class="muted" style="font-size: 11px; margin-top: 4px">
            {{ $t('page_settings_tokens.name_hint') }}
          </div>
        </a-form-item>

        <a-form-item :label="$t('page_settings_tokens.label_scope')">
          <a-select v-model:value="form.scope">
            <a-select-option value="reporter">
              {{ $t('page_settings_tokens.scope_reporter') }}
            </a-select-option>
            <a-select-option value="viewer">
              {{ $t('page_settings_tokens.scope_viewer') }}
            </a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item :label="$t('page_settings_tokens.label_expires')">
          <a-select v-model:value="form.expiresChoice">
            <a-select-option value="never">{{ $t('page_settings_tokens.expires_never') }}</a-select-option>
            <a-select-option value="30d">{{ $t('page_settings_tokens.expires_30d') }}</a-select-option>
            <a-select-option value="90d">{{ $t('page_settings_tokens.expires_90d') }}</a-select-option>
            <a-select-option value="1y">{{ $t('page_settings_tokens.expires_1y') }}</a-select-option>
          </a-select>
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- Plaintext "only shown once" modal -->
    <a-modal
      v-model:open="plaintextOpen"
      :title="$t('page_settings_tokens.modal_plaintext_title')"
      :closable="false"
      :mask-closable="false"
      :keyboard="false"
      :footer="null"
      width="560px"
    >
      <a-alert
        type="warning"
        show-icon
        :message="$t('page_settings_tokens.plaintext_warning')"
        :description="$t('page_settings_tokens.plaintext_warning_desc')"
        style="margin-bottom: 16px"
      />

      <div v-if="plaintextToken">
        <div class="muted" style="font-size: 12px; margin-bottom: 6px">{{ $t('page_settings_tokens.label_token') }}</div>
        <a-input-group compact style="display: flex">
          <a-input
            :value="plaintextToken.token"
            readonly
            style="flex: 1; font-family: ui-monospace, monospace"
            @focus="($event.target as HTMLInputElement).select()"
          />
          <a-button type="primary" @click="copyPlaintext">
            <template #icon><CopyOutlined /></template>
            {{ $t('page_settings_tokens.btn_copy') }}
          </a-button>
        </a-input-group>

        <a-descriptions :column="1" size="small" style="margin-top: 16px" bordered>
          <a-descriptions-item :label="$t('page_settings_tokens.label_name_desc')">
            {{ plaintextToken.name }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('page_settings_tokens.label_scope_desc')">
            <a-tag :color="scopeColor(plaintextToken.scope)">
              {{ plaintextToken.scope }}
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item :label="$t('page_settings_tokens.label_expires_desc')">
            <span v-if="plaintextToken.expires_at">
              {{ fmtTime(plaintextToken.expires_at) }}
            </span>
            <span v-else>{{ $t('page_settings_tokens.never_expires') }}</span>
          </a-descriptions-item>
        </a-descriptions>
      </div>

      <div style="display: flex; justify-content: flex-end; margin-top: 20px">
        <a-button type="primary" @click="closePlaintextModal">
          {{ $t('page_settings_tokens.btn_saved_close') }}
        </a-button>
      </div>
    </a-modal>

    <!-- SDK discovery card (#20) ——————————————————————————————————— -->
    <a-card
      size="small"
      style="margin-top: 16px"
      :title="$t('page_settings_sdk.card_title')"
    >
      <a-typography-paragraph style="margin-bottom: 12px">
        {{ $t('page_settings_sdk.description') }}
      </a-typography-paragraph>
      <a-typography-paragraph style="margin-bottom: 8px">
        <a
          href="https://github.com/leehom0123/argus/tree/main/client"
          target="_blank"
          rel="noopener noreferrer"
        >
          {{ $t('page_settings_sdk.link_text') }}
        </a>
      </a-typography-paragraph>
      <div class="muted" style="font-size: 12px; margin-bottom: 4px">
        {{ $t('page_settings_sdk.snippet_label') }}
      </div>
      <pre
        style="
          background: rgba(0, 0, 0, 0.04);
          padding: 10px 12px;
          border-radius: 4px;
          font-size: 12px;
          font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
          margin: 0;
          white-space: pre-wrap;
          word-break: break-all;
        "
>pip install argus-reporter

from argus import Reporter
with Reporter(project="myproj", argus_url="http://localhost:8000",
              token="em_live_...") as r:
    with r.job(model="my-model", dataset="my-data") as j:
        j.emit("job_epoch", epoch=0, train_loss=0.42)</pre>
    </a-card>
  </div>
</template>
