/**
 * useBatchCompactData — parallel-fetch all data needed by BatchCompactCard.
 *
 * Fetches: batch metadata, jobs list, epochs/latest, resources (last 20), eta
 * in a single Promise.all. Exposes reactive `data`, `loading`, `error` refs
 * and a `refresh()` function.
 */
import { ref, type Ref } from 'vue';
import {
  getBatch,
  listJobs,
  getBatchResources,
  getBatchEpochsLatest,
  type BatchCompactItem,
  type JobEpochLatest,
} from '../api/client';
import { http } from '../api/client';
import type { Batch, Job, ResourceSnapshot, BatchHealth, BatchETA } from '../types';

export interface BatchCompactData {
  batch: Batch;
  jobs: Job[];
  epochsLatest: JobEpochLatest[];
  resources: ResourceSnapshot[];
  health: BatchHealth;
  eta: BatchETA;
}

export interface UseBatchCompactDataReturn {
  data: Ref<BatchCompactData | null>;
  loading: Ref<boolean>;
  error: Ref<string | null>;
  refresh: () => Promise<void>;
}

async function fetchHealth(batchId: string): Promise<BatchHealth> {
  try {
    const { data } = await http.get<BatchHealth>(
      `/batches/${encodeURIComponent(batchId)}/health`,
    );
    return data;
  } catch {
    return {};
  }
}

async function fetchEta(batchId: string): Promise<BatchETA> {
  try {
    const { data } = await http.get<BatchETA>(
      `/batches/${encodeURIComponent(batchId)}/eta`,
    );
    return data;
  } catch {
    return {};
  }
}

export function useBatchCompactData(batchId: string): UseBatchCompactDataReturn {
  const data = ref<BatchCompactData | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);

  async function refresh(): Promise<void> {
    if (!batchId) return;
    loading.value = true;
    error.value = null;
    try {
      const [batch, jobs, epochsLatest, resourcesResp, health, eta] = await Promise.all([
        getBatch(batchId),
        listJobs(batchId),
        getBatchEpochsLatest(batchId).catch((): JobEpochLatest[] => []),
        getBatchResources(batchId, 20).catch((): { host: null; snapshots: ResourceSnapshot[] } => ({ host: null, snapshots: [] })),
        fetchHealth(batchId),
        fetchEta(batchId),
      ]);
      const resources = resourcesResp.snapshots;
      // Propagate host from the resources response if batch.host is absent.
      if (resourcesResp.host && !batch.host) {
        (batch as typeof batch & { host: string | null }).host = resourcesResp.host;
      }
      data.value = { batch, jobs, epochsLatest, resources, health, eta };
    } catch (e) {
      error.value = (e as Error)?.message ?? 'fetch error';
    } finally {
      loading.value = false;
    }
  }

  // Kick off on construction.
  void refresh();

  return { data, loading, error, refresh };
}

/**
 * Convert a server ``BatchCompactItem`` (from the bulk /batches/compact
 * endpoint) into the ``BatchCompactData`` shape the card consumes.
 *
 * The bulk endpoint does not include health / eta — those are best-effort
 * anyway and the card renders fine when both are empty objects. We also
 * reverse the ``resources`` array from newest-first (server order) to
 * oldest-first so the card's ``rs[rs.length - 1]`` = newest invariant
 * stays consistent with the per-batch endpoint path.
 */
export function compactItemToBatchCompactData(item: BatchCompactItem): BatchCompactData {
  const jobs = item.jobs as Job[];
  const epochsLatest: JobEpochLatest[] = (item.epochs_latest ?? []).map((e) => ({
    job_id: e.job_id,
    // Server omits the per-event timestamp; map to empty string so the
    // interface stays type-safe. The card doesn't render this field.
    timestamp: '',
    epoch: e.epoch,
    train_loss: e.train_loss ?? null,
    val_loss: e.val_loss ?? null,
    lr: e.lr ?? null,
    val_loss_trace: e.val_loss_trace ?? [],
  }));
  // Newest → oldest in the server payload. The card expects
  // ``rs[rs.length - 1]`` to be the newest snapshot, so reverse.
  const resources: ResourceSnapshot[] = (item.resources ?? [])
    .map((r) => ({
      timestamp: r.timestamp,
      host: r.host,
      gpu_util_pct: r.gpu_util_pct ?? null,
      gpu_mem_mb: r.gpu_mem_mb ?? null,
      gpu_mem_total_mb: r.gpu_mem_total_mb ?? null,
      gpu_temp_c: r.gpu_temp_c ?? null,
      cpu_util_pct: r.cpu_util_pct ?? null,
      ram_mb: r.ram_mb ?? null,
      ram_total_mb: r.ram_total_mb ?? null,
      disk_free_mb: r.disk_free_mb ?? null,
      pid: null,
    }))
    .reverse();
  return {
    batch: item.batch as Batch,
    jobs,
    epochsLatest,
    resources,
    health: {},
    eta: {},
  };
}
