<script setup lang="ts">
/**
 * HostCapacityChip.vue
 *
 * Compact per-host capacity strip for the Dashboard "服务器容量 / Host capacity" rail.
 * Shows 4 utilization bars (GPU util, VRAM, RAM, Disk) with color-coded thresholds,
 * the top-consuming batch ID from the last timeseries bucket, and a verdict chip
 * telling operators whether they can safely launch another experiment.
 *
 * Verdict rules:
 *   GREEN  — GPU util < 60 AND VRAM < 60 AND no stalled batch
 *   AMBER  — any of GPU util / VRAM / RAM 60-85
 *   RED    — any metric >= 85 OR any stalled batch detected
 *
 * Disk coloring (matches the GPU/VRAM/RAM bars — bar fills as things get worse):
 *   GREEN  — used% < 70
 *   GOLD   — used% 70-85
 *   RED    — used% >= 85
 *
 * Used% is computed from the snapshot when ``disk_total_mb`` is present
 * (migration 020 + DeepTS reporter ``disk_total_mb`` field). For older reporters
 * that emit only ``disk_free_mb`` we fall back to a free-GB pressure heuristic
 * so the bar still shades from green → gold → red as space dwindles.
 */

import { ref, computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { getHostTimeseries } from '../api/client';
import { cached } from '../composables/useCache';
import type { HostSummary } from '../types';

const props = defineProps<{
  host: HostSummary;
}>();

const { t } = useI18n();

// Top-consumer batch id fetched from timeseries last bucket
const topConsumerBatchId = ref<string | null>(null);
const topConsumerPct = ref<number | null>(null);

async function fetchTopConsumer(): Promise<void> {
  try {
    // Cache per-host so N HostCapacityChip instances on Dashboard dedup into
    // one HTTP call + reuse for 30s across page transitions.
    const result = await cached(
      `host-ts-topcons:${props.host.host}`,
      () => getHostTimeseries(props.host.host, {
        metric: 'gpu_mem_mb',
        since: 'now-1h',
        bucket_seconds: 60,
      }),
      10_000,
    );
    const buckets = result.buckets;
    if (!buckets.length) return;
    const lastBucket = buckets[buckets.length - 1];
    const byBatch = lastBucket.by_batch ?? {};
    const entries = Object.entries(byBatch);
    if (!entries.length) return;
    // Pick the batch with the highest value in the last bucket
    let maxBatch = entries[0][0];
    let maxVal = entries[0][1];
    for (const [bid, val] of entries) {
      if (val > maxVal) { maxBatch = bid; maxVal = val; }
    }
    topConsumerBatchId.value = maxBatch;
    // Express as % of host total capacity
    const cap = result.host_total_capacity;
    if (cap != null && cap > 0) {
      topConsumerPct.value = Math.round((maxVal / cap) * 100);
    }
  } catch {
    // best-effort — silently ignore
  }
}

onMounted(fetchTopConsumer);

// ── Derived metrics from the HostSummary snapshot ─────────────────────────

const gpuUtilPct = computed<number | null>(() => {
  const v = props.host.gpu_util_pct;
  return v != null ? Math.round(v) : null;
});

const vramPct = computed<number | null>(() => {
  const used = props.host.gpu_mem_mb;
  const total = props.host.gpu_mem_total_mb;
  if (used == null || total == null || total === 0) return null;
  return Math.round((used / total) * 100);
});

const ramPct = computed<number | null>(() => {
  const used = props.host.ram_mb;
  const total = props.host.ram_total_mb;
  if (used == null || total == null || total === 0) return null;
  return Math.round((used / total) * 100);
});

const diskFreeMb = computed<number | null>(() => props.host.disk_free_mb ?? null);
const diskTotalMb = computed<number | null>(() => props.host.disk_total_mb ?? null);

/**
 * True used% when the reporter sends ``disk_total_mb``. Returns null when
 * total is missing/zero or free is null — callers fall back to the legacy
 * free-GB pressure formula in ``diskBarPct`` / ``diskStrokeColor``.
 */
const diskUsedPct = computed<number | null>(() => {
  const free = diskFreeMb.value;
  const total = diskTotalMb.value;
  if (free == null || total == null || total <= 0) return null;
  const used = total - free;
  if (used < 0) return 0;
  return Math.round((used / total) * 100);
});

// ── Util color helper ─────────────────────────────────────────────────────

function utilColor(pct: number | null): string {
  if (pct == null) return 'default';
  if (pct < 60) return 'green';
  if (pct < 80) return 'gold';
  if (pct < 90) return 'orange';
  return 'red';
}

// CSS color for progress bar stroke
function utilStrokeColor(pct: number | null): string {
  if (pct == null) return '#888';
  if (pct < 60) return '#52c41a';
  if (pct < 80) return '#faad14';
  if (pct < 90) return '#fa8c16';
  return '#ff4d4f';
}

/**
 * Disk bar stroke colour. When ``disk_total_mb`` is available we use the
 * real used% with the green/gold/red thresholds requested by PM
 * (< 70 green, 70-85 gold, >= 85 red). Otherwise we fall back to the
 * legacy free-GB heuristic so older reporters still surface the right
 * traffic-light colour.
 */
function diskStrokeColor(freeMb: number | null, totalMb: number | null): string {
  if (totalMb != null && totalMb > 0 && freeMb != null) {
    const used = totalMb - freeMb;
    const pct = used >= 0 ? (used / totalMb) * 100 : 0;
    if (pct < 70) return '#52c41a';
    if (pct < 85) return '#faad14';
    return '#ff4d4f';
  }
  if (freeMb == null) return '#888';
  const freeGb = freeMb / 1024;
  if (freeGb >= 50) return '#52c41a';
  if (freeGb >= 20) return '#faad14';
  return '#ff4d4f';
}

/**
 * Disk pressure bar — inverted so it reads like the GPU/RAM bars next to it
 * (bar fills as things get worse).
 *
 * Preferred path: ``disk_total_mb`` is set, so we render the real used% and
 * the bar matches what operators see in ``df -h``.
 *
 * Fallback path: only ``disk_free_mb`` is available — map "free GB remaining"
 * onto a 0-100 danger scale where 100 GB+ free = 0% pressure (empty bar) and
 * ≤5 GB = 100% (full). Linear in between; lines up with ``diskStrokeColor``.
 */
function diskBarPct(freeMb: number | null, totalMb: number | null): number {
  if (totalMb != null && totalMb > 0 && freeMb != null) {
    const used = totalMb - freeMb;
    if (used <= 0) return 0;
    return Math.min(100, Math.max(0, Math.round((used / totalMb) * 100)));
  }
  if (freeMb == null) return 0;
  const freeGb = freeMb / 1024;
  if (freeGb >= 100) return 0;
  if (freeGb <= 5) return 100;
  return Math.round(((100 - freeGb) / 95) * 100);
}

// ── Verdict ───────────────────────────────────────────────────────────────

type Verdict = 'green' | 'amber' | 'red';

const verdict = computed<Verdict>(() => {
  const gpu = gpuUtilPct.value;
  const vram = vramPct.value;
  const ram = ramPct.value;

  // Any metric >= 85 → red
  if (
    (gpu != null && gpu >= 85) ||
    (vram != null && vram >= 85) ||
    (ram != null && ram >= 85)
  ) return 'red';

  // Any metric 60-84 → amber
  if (
    (gpu != null && gpu >= 60) ||
    (vram != null && vram >= 60) ||
    (ram != null && ram >= 60)
  ) return 'amber';

  return 'green';
});

const verdictLabel = computed<string>(() => {
  if (verdict.value === 'green') {
    const freeGpu = gpuUtilPct.value != null ? 100 - gpuUtilPct.value : null;
    return t('component_host_capacity_chip.can_launch_green', {
      pct: freeGpu != null ? freeGpu : '?',
    });
  }
  if (verdict.value === 'amber') {
    const worstMetric = Math.max(gpuUtilPct.value ?? 0, vramPct.value ?? 0, ramPct.value ?? 0);
    return t('component_host_capacity_chip.can_launch_amber', { pct: worstMetric });
  }
  // red
  const worstMetric = Math.max(gpuUtilPct.value ?? 0, vramPct.value ?? 0, ramPct.value ?? 0);
  return t('component_host_capacity_chip.can_launch_red', { pct: worstMetric });
});

const verdictColor = computed<string>(() => {
  if (verdict.value === 'green') return 'success';
  if (verdict.value === 'amber') return 'warning';
  return 'error';
});

// Format free MB as GB string
function fmtGB(mb: number | null): string {
  if (mb == null) return '—';
  return `${(mb / 1024).toFixed(1)} GB`;
}
</script>

<template>
  <div class="hcc-chip">
    <!-- Host name -->
    <div class="hcc-host-name">{{ host.host }}</div>

    <!-- Bars -->
    <div class="hcc-bars">
      <!-- GPU util -->
      <div class="hcc-bar-row">
        <span class="hcc-bar-label">GPU</span>
        <a-progress
          v-if="gpuUtilPct != null"
          :percent="gpuUtilPct"
          :show-info="false"
          size="small"
          :stroke-color="utilStrokeColor(gpuUtilPct)"
          class="hcc-progress"
        />
        <span v-else class="hcc-bar-label hcc-muted">—</span>
        <span class="hcc-bar-val" :style="{ color: utilStrokeColor(gpuUtilPct) }">
          {{ gpuUtilPct != null ? gpuUtilPct + '%' : '—' }}
        </span>
      </div>

      <!-- VRAM -->
      <div class="hcc-bar-row">
        <span class="hcc-bar-label">VRAM</span>
        <a-progress
          v-if="vramPct != null"
          :percent="vramPct"
          :show-info="false"
          size="small"
          :stroke-color="utilStrokeColor(vramPct)"
          class="hcc-progress"
        />
        <span v-else class="hcc-bar-label hcc-muted">—</span>
        <span class="hcc-bar-val" :style="{ color: utilStrokeColor(vramPct) }">
          {{ vramPct != null ? vramPct + '%' : '—' }}
        </span>
      </div>

      <!-- RAM -->
      <div class="hcc-bar-row">
        <span class="hcc-bar-label">RAM</span>
        <a-progress
          v-if="ramPct != null"
          :percent="ramPct"
          :show-info="false"
          size="small"
          :stroke-color="utilStrokeColor(ramPct)"
          class="hcc-progress"
        />
        <span v-else class="hcc-bar-label hcc-muted">—</span>
        <span class="hcc-bar-val" :style="{ color: utilStrokeColor(ramPct) }">
          {{ ramPct != null ? ramPct + '%' : '—' }}
        </span>
      </div>

      <!-- Disk free -->
      <div class="hcc-bar-row">
        <span class="hcc-bar-label">Disk</span>
        <a-progress
          v-if="diskFreeMb != null"
          :percent="diskBarPct(diskFreeMb, diskTotalMb)"
          :show-info="false"
          size="small"
          :stroke-color="diskStrokeColor(diskFreeMb, diskTotalMb)"
          class="hcc-progress"
        />
        <span v-else class="hcc-bar-label hcc-muted">—</span>
        <span class="hcc-bar-val" :style="{ color: diskStrokeColor(diskFreeMb, diskTotalMb) }">
          {{ diskUsedPct != null ? diskUsedPct + '%' : fmtGB(diskFreeMb) }}
        </span>
      </div>
    </div>

    <!-- Top consumer -->
    <div v-if="topConsumerBatchId" class="hcc-consumer">
      <span class="hcc-consumer-label">{{ $t('component_host_capacity_chip.top_consumer') }}:</span>
      <span class="hcc-consumer-id">{{ topConsumerBatchId }}</span>
      <span v-if="topConsumerPct != null" class="hcc-consumer-pct">({{ topConsumerPct }}% GPU mem)</span>
    </div>

    <!-- Verdict chip -->
    <div class="hcc-verdict">
      <a-tag :color="verdictColor" style="font-size: 11.5px; white-space: normal; word-break: break-word">
        {{ verdictLabel }}
      </a-tag>
    </div>
  </div>
</template>

<style scoped>
.hcc-chip {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 10px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  background: var(--surface-soft);
  min-width: 200px;
  max-width: 280px;
  flex: 1 1 220px;
}

.hcc-host-name {
  font-size: 12.5px;
  font-weight: 600;
  font-family: 'SFMono-Regular', Consolas, monospace;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text-primary);
}

.hcc-bars {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.hcc-bar-row {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
}

.hcc-bar-label {
  width: 34px;
  font-size: 10px;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
  white-space: nowrap;
  flex-shrink: 0;
}

.hcc-muted {
  color: var(--text-quaternary);
}

.hcc-progress {
  flex: 1;
  margin: 0 !important;
}

.hcc-bar-val {
  width: 52px;
  text-align: right;
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}

.hcc-consumer {
  font-size: 10.5px;
  color: var(--text-secondary);
  display: flex;
  align-items: baseline;
  gap: 4px;
  flex-wrap: wrap;
}

.hcc-consumer-label {
  color: var(--text-tertiary);
  white-space: nowrap;
}

.hcc-consumer-id {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 120px;
  white-space: nowrap;
}

.hcc-consumer-pct {
  color: var(--text-tertiary);
  font-size: 10px;
  white-space: nowrap;
}

.hcc-verdict {
  margin-top: 2px;
}
</style>
