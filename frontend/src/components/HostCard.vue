<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import { WarningFilled } from '@ant-design/icons-vue';
import { useRouter } from 'vue-router';
import type { HostSummary } from '../types';
import { fmtRelative } from '../utils/format';
import { statusBorderColor, hostAggregateStatus } from '../utils/status';
import { getStatusColor } from '../composables/useStatusColor';

const { t } = useI18n();
const props = defineProps<{ host: HostSummary }>();
const router = useRouter();

function pct(v: number | null | undefined): number {
  if (v === null || v === undefined || !Number.isFinite(v)) return 0;
  return Math.min(100, Math.max(0, v));
}

function ratio(used?: number | null, total?: number | null): number {
  if (!used || !total || total <= 0) return 0;
  return Math.min(100, Math.max(0, (used / total) * 100));
}

const gpuWarn = computed(() => (props.host.gpu_temp_c ?? 0) > 85);
const diskWarn = computed(
  () => props.host.disk_free_mb !== null && props.host.disk_free_mb !== undefined && props.host.disk_free_mb < 10 * 1024,
);

/** Aggregate status for the host card's left-border colour. */
const hostStatus = computed(() => hostAggregateStatus(props.host));

/**
 * Status colour tokens for the unified 5-colour scheme (#125). Powers
 * the ``aria-label`` on the outer card so screen readers can announce
 * the host's aggregate health alongside the visual border colour.
 */
const statusTokens = computed(() =>
  getStatusColor('host', hostStatus.value, { host: props.host }),
);

const ramPct = computed(() => ratio(props.host.ram_mb, props.host.ram_total_mb));
const vramPct = computed(() => ratio(props.host.gpu_mem_mb, props.host.gpu_mem_total_mb));
const diskPct = computed(() => {
  const free = props.host.disk_free_mb;
  const total = props.host.disk_total_mb;
  if (!total || total <= 0) return 0;
  return Math.min(100, Math.max(0, ((total - (free ?? 0)) / total) * 100));
});
const diskStrokeColor = computed(() => {
  const p = diskPct.value;
  if (!props.host.disk_total_mb) return '#b37feb';
  if (p > 90) return '#ff4d4f';
  if (p > 80) return '#ffa940';
  return '#b37feb';
});

function fmtMB(mb?: number | null): string {
  if (mb === null || mb === undefined) return '—';
  if (mb < 1024) return `${Math.round(mb)}MB`;
  return `${(mb / 1024).toFixed(1)}GB`;
}

function open() {
  router.push(`/hosts/${encodeURIComponent(props.host.host)}`);
}
</script>

<template>
  <a-card
    size="small"
    hoverable
    :bodyStyle="{ padding: '10px 12px' }"
    :style="{
      cursor: 'pointer',
      borderLeft: `4px solid ${statusBorderColor(hostStatus)}`,
    }"
    :aria-label="statusTokens.aria"
    :data-status-bucket="statusTokens.bucket"
    @click="open"
  >
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px">
      <div style="font-weight: 500; font-size: 13px">{{ host.host }}</div>
      <a-tooltip
        v-if="gpuWarn || diskWarn || (host.warnings?.length ?? 0) > 0"
        :title="
          [
            gpuWarn ? `GPU ${host.gpu_temp_c}°C` : null,
            diskWarn ? `Disk ${fmtMB(host.disk_free_mb)} free` : null,
            ...(host.warnings ?? []),
          ]
            .filter(Boolean)
            .join(' · ')
        "
      >
        <WarningFilled style="color: #ff4d4f" />
      </a-tooltip>
    </div>

    <!-- Core bars -->
    <div class="bar-row">
      <span class="bar-label">GPU</span>
      <a-progress
        :percent="pct(host.gpu_util_pct)"
        size="small"
        :show-info="false"
        :stroke-color="gpuWarn ? '#ff4d4f' : '#4096ff'"
      />
      <span class="bar-val">
        {{ host.gpu_util_pct ?? '—' }}%
        <span
          v-if="host.gpu_temp_c != null"
          :style="{ marginLeft: '4px', color: gpuWarn ? '#ff7875' : 'var(--text-secondary)' }"
        >
          {{ $t('component_host_card.gpu_temp_c', { c: Math.round(host.gpu_temp_c) }) }}
        </span>
      </span>
    </div>
    <div class="bar-row">
      <span class="bar-label">VRAM</span>
      <a-progress :percent="vramPct" size="small" :show-info="false" stroke-color="#36cfc9" />
      <span class="bar-val">{{ fmtMB(host.gpu_mem_mb) }}/{{ fmtMB(host.gpu_mem_total_mb) }}</span>
    </div>
    <div class="bar-row">
      <span class="bar-label">CPU</span>
      <a-progress :percent="pct(host.cpu_util_pct)" size="small" :show-info="false" stroke-color="#73d13d" />
      <span class="bar-val">{{ host.cpu_util_pct ?? '—' }}%</span>
    </div>
    <div class="bar-row">
      <span class="bar-label">RAM</span>
      <a-progress :percent="ramPct" size="small" :show-info="false" stroke-color="#ffa940" />
      <span class="bar-val">{{ fmtMB(host.ram_mb) }}/{{ fmtMB(host.ram_total_mb) }}</span>
    </div>
    <div class="bar-row">
      <span class="bar-label">DISK</span>
      <a-progress
        :percent="host.disk_total_mb ? diskPct : 0"
        size="small"
        :show-info="false"
        :stroke-color="diskStrokeColor"
      />
      <span class="bar-val">
        <template v-if="host.disk_total_mb">
          {{ fmtMB(host.disk_free_mb) }} {{ $t('component_host_card.disk_free_label') }}
        </template>
        <template v-else>{{ fmtMB(host.disk_free_mb) }}</template>
      </span>
    </div>

    <div
      class="muted"
      style="font-size: 11px; margin-top: 6px; display: flex; justify-content: space-between"
    >
      <span>{{ $t('component_host_card.jobs_running', { count: host.running_jobs ?? 0 }) }}</span>
      <span>{{ fmtRelative(host.last_seen) || '—' }}</span>
    </div>

    <!-- v0.1.3 density: running jobs top-5 chip list. Hidden when empty. -->
    <div
      v-if="(host.running_jobs_top5?.length ?? 0) > 0"
      class="hc-running-row"
    >
      <span class="hc-running-label">
        {{ $t('component_host_card.running_jobs_top5_label') }}:
      </span>
      <span
        v-for="(j, idx) in host.running_jobs_top5"
        :key="j.job_id"
      >
        <span class="hc-running-chip">
          {{ j.model ?? '—' }}×{{ j.dataset ?? '—' }}
          <span v-if="j.user || j.pid != null" class="hc-running-meta">
            (<template v-if="j.user">{{ j.user }}</template
            ><template v-if="j.user && j.pid != null">, </template
            ><template v-if="j.pid != null">pid {{ j.pid }}</template>)
          </span>
        </span>
        <span
          v-if="idx < (host.running_jobs_top5?.length ?? 0) - 1"
          class="hc-running-sep"
        >·</span>
      </span>
    </div>
  </a-card>
</template>

<style scoped>
.bar-row {
  display: grid;
  grid-template-columns: 44px 1fr 110px;
  gap: 8px;
  align-items: center;
  font-size: 11px;
  margin-bottom: 2px;
}
.bar-label {
  color: var(--text-secondary);
}
.bar-val {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}
.hc-running-row {
  margin-top: 5px;
  font-size: 10.5px;
  color: var(--text-secondary);
  line-height: 1.45;
  font-family: 'SFMono-Regular', Consolas, monospace;
  word-break: break-all;
}
.hc-running-label {
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
  font-size: 10px;
  margin-right: 4px;
}
.hc-running-chip {
  white-space: nowrap;
}
.hc-running-meta {
  color: var(--text-tertiary);
  margin-left: 1px;
}
.hc-running-sep {
  color: var(--text-quaternary);
  margin: 0 4px;
}
</style>
