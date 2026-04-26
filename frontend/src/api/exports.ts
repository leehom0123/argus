// CSV export endpoints (requirements §16.8 / §17).
//
// All four endpoints return `text/csv` as a Blob. We trigger a browser
// download by synthesising an anchor click — no extra dependency needed.
// Filename is derived from the response Content-Disposition when present,
// otherwise we fall back to a sensible default per endpoint.

import { http } from './client';

function filenameFromHeaders(
  headers: Record<string, unknown> | undefined,
  fallback: string,
): string {
  const cd = (headers?.['content-disposition'] as string | undefined) ?? '';
  // RFC 5987 / simple quoted form: filename="batch_xxx.csv"
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  if (m && m[1]) {
    try {
      return decodeURIComponent(m[1]);
    } catch {
      return m[1];
    }
  }
  return fallback;
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Give the browser a tick to start the download before revoking.
  setTimeout(() => URL.revokeObjectURL(url), 500);
}

async function fetchAndDownload(url: string, fallbackName: string): Promise<void> {
  const resp = await http.get(url, { responseType: 'blob' });
  const blob = resp.data as Blob;
  const name = filenameFromHeaders(resp.headers as Record<string, unknown>, fallbackName);
  downloadBlob(blob, name);
}

export async function exportBatchCsv(batchId: string): Promise<void> {
  await fetchAndDownload(
    `/batches/${encodeURIComponent(batchId)}/export.csv`,
    `batch_${batchId}.csv`,
  );
}

export async function exportProjectCsv(project: string): Promise<void> {
  await fetchAndDownload(
    `/projects/${encodeURIComponent(project)}/export.csv`,
    `project_${project}.csv`,
  );
}

export async function exportProjectRawCsv(project: string): Promise<void> {
  await fetchAndDownload(
    `/projects/${encodeURIComponent(project)}/export-raw.csv`,
    `project_${project}_raw.csv`,
  );
}

export async function exportCompareCsv(batchIds: string[]): Promise<void> {
  const qs = `batches=${encodeURIComponent(batchIds.join(','))}`;
  await fetchAndDownload(
    `/compare/export.csv?${qs}`,
    `compare_${batchIds.join('_')}.csv`,
  );
}
