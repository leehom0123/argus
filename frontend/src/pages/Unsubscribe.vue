<script setup lang="ts">
/**
 * /unsubscribe?token=... — anonymous unsubscribe landing page.
 *
 * A user clicks the one-click unsubscribe link in a sent email. We hit
 * POST /api/unsubscribe?token=<signed-opaque-token> on mount. The backend
 * validates the signature, looks up the (user_id, event_type) pair and
 * sets `enabled=false` for that subscription. No auth header is required.
 *
 * States:
 *   - loading: waiting for the server
 *   - success: shows the event type we unsubscribed from
 *   - error:   invalid / expired / already-consumed token, or network fail
 */

import { onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import AuthLayout from '../components/AuthLayout.vue';
import { unsubscribeWithToken } from '../api/email';

type State = 'loading' | 'success' | 'error';

const route = useRoute();
const state = ref<State>('loading');
const detail = ref<string>('');

onMounted(async () => {
  const token = typeof route.query.token === 'string' ? route.query.token : '';
  if (!token) {
    state.value = 'error';
    detail.value = 'No unsubscribe token found in the link.';
    return;
  }
  try {
    const res = await unsubscribeWithToken(token);
    detail.value = res.detail ?? '';
    state.value = res.ok ? 'success' : 'error';
  } catch {
    state.value = 'error';
    detail.value = '';
  }
});
</script>

<template>
  <AuthLayout>
    <a-card :bordered="false" class="auth-card">
      <template v-if="state === 'loading'">
        <div style="text-align: center; padding: 24px 0">
          <a-spin size="large" />
          <p style="margin-top: 16px">
            {{ $t('page_unsubscribe.loading') }}
          </p>
        </div>
      </template>

      <template v-else-if="state === 'success'">
        <a-result
          status="success"
          :title="$t('page_unsubscribe.success_title')"
          :sub-title="detail || $t('page_unsubscribe.success_desc_generic')"
        >
          <template #extra>
            <div style="display: flex; gap: 8px; justify-content: center; flex-wrap: wrap">
              <router-link to="/login">
                <a-button>{{ $t('page_unsubscribe.go_sign_in') }}</a-button>
              </router-link>
              <router-link to="/settings/notifications">
                <a-button type="primary">
                  {{ $t('page_unsubscribe.resubscribe') }}
                </a-button>
              </router-link>
            </div>
            <p class="muted" style="margin-top: 16px; font-size: 12px">
              {{ $t('page_unsubscribe.success_footer') }}
            </p>
          </template>
        </a-result>
      </template>

      <template v-else>
        <a-result
          status="error"
          :title="$t('page_unsubscribe.error_title')"
          :sub-title="detail || $t('page_unsubscribe.error_desc_default')"
        >
          <template #extra>
            <router-link to="/login">
              <a-button type="primary">
                {{ $t('page_unsubscribe.go_sign_in') }}
              </a-button>
            </router-link>
          </template>
        </a-result>
      </template>
    </a-card>
  </AuthLayout>
</template>
