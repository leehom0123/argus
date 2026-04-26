<script setup lang="ts">
import { computed } from 'vue';
import { useRouter } from 'vue-router';
import { useI18n } from 'vue-i18n';
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  MinusOutlined,
  ShareAltOutlined,
  FieldTimeOutlined,
  WarningFilled,
  CloseCircleFilled,
  ClockCircleFilled,
  AimOutlined,
  DatabaseOutlined,
  UserOutlined,
} from '@ant-design/icons-vue';
import type { ActiveBatchCard } from '../types';
import { fmtDuration } from '../utils/format';
import { statusBorderColor } from '../utils/status';
import { getStatusColor } from '../composables/useStatusColor';
import StatusTag from './StatusTag.vue';
import ProgressInline from './ProgressInline.vue';
import MiniSparkline from './MiniSparkline.vue';
import PinButton from './PinButton.vue';

/**
 * BatchCard renders an Active-tab style card for a running or compare-pool
 * batch. `compact` mode hides the running-jobs slots list and just shows
 * header + metrics (used in Compare view).
 */
const props = withDefaults(
  defineProps<{
    data: ActiveBatchCard;
    compact?: boolean;
    /** Hide Pin button (Compare view already implies pin). */
    hidePin?: boolean;
    /** Hide Share button (Compare view). */
    hideShare?: boolean;
  }>(),
  { compact: false, hidePin: false, hideShare: false },
);

const emit = defineEmits<{
  (e: 'share', batchId: string): void;
}>();

const { t } = useI18n();
const router = useRouter();

const hasRunningJobs = computed(() => (props.data.running_jobs?.length ?? 0) > 0);
const progressPercent = computed(() => {
  const d = props.data;
  if (!d.n_total || d.n_total <= 0) return 0;
  return Math.round(((d.n_done ?? 0) / d.n_total) * 100);
});

/**
 * Status colour tokens for the unified 5-colour scheme (#125). The
 * ``is_stalled`` heartbeat flag is treated as a stalled override for
 * the aria-label so screen readers announce "Status: Stalled" the same
 * way the visible amber chip flags it. Border colour stays driven by
 * the canonical ``data.status`` to avoid surprising existing tests /
 * snapshots that pin the border-tracks-status invariant.
 */
const statusTokens = computed(() => {
  const status = props.data.is_stalled ? 'stalled' : props.data.status;
  return getStatusColor('batch', status);
});

function fmtMB(mb?: number | null): string {
  if (mb === null || mb === undefined) return '—';
  if (mb < 1024) return `${Math.round(mb)}MB`;
  return `${(mb / 1024).toFixed(1)}GB`;
}

function trendIcon(trend?: 'down' | 'up' | 'flat' | null) {
  if (trend === 'down') return ArrowDownOutlined;
  if (trend === 'up') return ArrowUpOutlined;
  return MinusOutlined;
}
function trendColor(trend?: 'down' | 'up' | 'flat' | null) {
  if (trend === 'down') return '#52c41a'; // descending loss = good
  if (trend === 'up') return '#ff4d4f';
  // Flat / unknown — fall through to the theme-aware tertiary text token
  // so we don't ship a literal white pixel on a white card (#120).
  return 'var(--text-tertiary)';
}

function open() {
  router.push(`/batches/${encodeURIComponent(props.data.batch_id)}`);
}

function viewMatrix(ev: MouseEvent) {
  ev.stopPropagation();
  router.push(`/batches/${encodeURIComponent(props.data.batch_id)}?tab=matrix`);
}

function viewJobs(ev: MouseEvent) {
  ev.stopPropagation();
  router.push(`/batches/${encodeURIComponent(props.data.batch_id)}?tab=jobs`);
}

function onShare(ev: MouseEvent) {
  ev.stopPropagation();
  emit('share', props.data.batch_id);
}
</script>

<template>
  <a-card
    size="small"
    hoverable
    :bodyStyle="{ padding: '14px 16px' }"
    :style="{
      cursor: 'pointer',
      borderLeft: `4px solid ${statusBorderColor(data.status)}`,
    }"
    :aria-label="statusTokens.aria"
    :data-status-bucket="statusTokens.bucket"
    @click="open"
  >
    <!-- Header row -->
    <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
      <StatusTag :status="data.status" />
      <div
        style="font-weight: 600; font-family: 'SFMono-Regular', Consolas, monospace; font-size: 13px; min-width: 0; flex: 1;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis"
      >
        {{ data.batch_id }}
      </div>
      <span v-if="data.is_stalled" class="muted" style="color: #fa8c16; font-size: 12px">
        <ClockCircleFilled /> {{ $t('component_batch_card.stalled') }}
        <template v-if="data.last_event_age_s">
          ({{ fmtDuration(data.last_event_age_s) }})
        </template>
      </span>
    </div>

    <div class="muted" style="font-size: 11px; margin-top: 2px; display: flex; gap: 10px; flex-wrap: wrap">
      <span v-if="data.user"><UserOutlined /> {{ data.user }}</span>
      <span v-if="data.host"><DatabaseOutlined /> {{ data.host }}</span>
      <span v-if="data.project && !compact">{{ data.project }}</span>
    </div>

    <!-- Progress -->
    <div style="margin-top: 10px">
      <ProgressInline
        :done="data.n_done ?? 0"
        :total="data.n_total ?? 0"
        :failed="data.n_failed ?? 0"
        :width="'100%'"
      />
      <div
        class="muted"
        style="font-size: 11px; margin-top: 2px; display: flex; gap: 10px; flex-wrap: wrap"
      >
        <span>{{ progressPercent }}%</span>
        <span v-if="data.elapsed_s">
          <FieldTimeOutlined /> {{ $t('component_batch_card.elapsed', { duration: fmtDuration(data.elapsed_s) }) }}
        </span>
        <span v-if="data.eta_s != null">{{ $t('component_batch_card.eta', { duration: fmtDuration(data.eta_s) }) }}</span>
      </div>
    </div>

    <!-- Running jobs slots -->
    <div v-if="!compact && hasRunningJobs" style="margin-top: 10px">
      <div class="muted" style="font-size: 10.5px; text-transform: uppercase; margin-bottom: 4px">
        {{ $t('component_batch_card.running_label', { count: data.running_jobs?.length ?? 0 }) }}
      </div>
      <div
        v-for="j in data.running_jobs"
        :key="j.job_id"
        class="slot-row"
      >
        <div class="slot-tag">
          {{ j.model ?? '—' }} × {{ j.dataset ?? '—' }}
        </div>
        <div class="slot-epoch">
          <template v-if="j.epoch != null">
            ep {{ j.epoch }}<template v-if="j.total_epochs">/{{ j.total_epochs }}</template>
          </template>
        </div>
        <div class="slot-loss">
          <template v-if="j.val_loss != null">
            {{ j.val_loss.toFixed(4) }}
            <component
              :is="trendIcon(j.trend)"
              :style="{ color: trendColor(j.trend), marginLeft: '2px', fontSize: '10px' }"
            />
          </template>
        </div>
        <div class="slot-spark">
          <MiniSparkline v-if="(j.loss_trace?.length ?? 0) > 1" :data="j.loss_trace ?? []" :height="20" />
        </div>
      </div>
    </div>

    <!-- Spark + GPU/VRAM footer -->
    <div
      v-if="!compact"
      style="margin-top: 8px; display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center"
    >
      <div>
        <MiniSparkline
          v-if="(data.sparkline?.length ?? 0) > 1"
          :data="data.sparkline as number[]"
          :height="26"
          area
        />
      </div>
      <div class="muted" style="font-size: 11px; text-align: right; min-width: 150px">
        <div>
          GPU {{ data.gpu_util_pct ?? '—' }}% ·
          VRAM {{ fmtMB(data.gpu_mem_mb) }}
        </div>
        <div>{{ $t('component_batch_card.disk_free', { size: fmtMB(data.disk_free_mb) }) }}</div>
      </div>
    </div>

    <!-- Warnings -->
    <div v-if="(data.n_failed ?? 0) > 0 || (data.warnings?.length ?? 0) > 0" style="margin-top: 8px">
      <a-tag v-if="(data.n_failed ?? 0) > 0" color="red" style="margin-right: 4px">
        <CloseCircleFilled /> {{ $t('component_batch_card.failed_count', { count: data.n_failed }) }}
      </a-tag>
      <a-tag v-for="w in (data.warnings ?? [])" :key="w" color="orange" style="margin-right: 4px">
        <WarningFilled /> {{ w }}
      </a-tag>
      <a-tag v-if="data.best_so_far_pct" color="blue">
        <AimOutlined /> {{ data.best_so_far_pct.toFixed(1) }}% vs best
      </a-tag>
    </div>

    <!-- Actions -->
    <div style="margin-top: 10px; display: flex; gap: 6px; flex-wrap: wrap">
      <a-button size="small" @click="viewMatrix">{{ $t('component_batch_card.btn_matrix') }}</a-button>
      <a-button size="small" @click="viewJobs">{{ $t('component_batch_card.btn_jobs') }}</a-button>
      <a-button v-if="!hideShare" size="small" @click="onShare">
        <template #icon><ShareAltOutlined /></template>
        {{ $t('component_batch_card.btn_share') }}
      </a-button>
      <PinButton v-if="!hidePin" :batch-id="data.batch_id" />
    </div>
  </a-card>
</template>

<style scoped>
.slot-row {
  display: grid;
  grid-template-columns: 1fr 60px 80px 64px;
  gap: 6px;
  align-items: center;
  font-size: 11.5px;
  padding: 2px 0;
  border-top: 1px dashed var(--border-soft);
}
.slot-row:first-child {
  border-top: none;
}
.slot-tag {
  font-family: 'SFMono-Regular', Consolas, monospace;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.slot-epoch {
  color: var(--text-secondary);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.slot-loss {
  color: var(--text-primary);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
</style>
