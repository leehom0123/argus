import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';

dayjs.extend(relativeTime);

export function fmtTime(iso?: string | null): string {
  if (!iso) return '—';
  const d = dayjs(iso);
  if (!d.isValid()) return iso;
  return d.format('YYYY-MM-DD HH:mm:ss');
}

export function fmtRelative(iso?: string | null): string {
  if (!iso) return '';
  const d = dayjs(iso);
  if (!d.isValid()) return '';
  return d.fromNow();
}

export function fmtDuration(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

/**
 * Human-readable byte size — "1.2 KB", "4.7 MB", "3.1 GB", …
 *
 * Uses 1024-based units (KiB/MiB/GiB convention) but the short labels (KB, MB,
 * GB) because that's what the rest of the UI uses — see ``fmtGB`` / ``fmtDiskGB``
 * in BatchDetail.vue. Negative / non-finite / null inputs → "—".
 */
export function fmtBytes(bytes?: number | null): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
}

export function durationBetween(start?: string | null, end?: string | null): string {
  if (!start) return '—';
  const s = dayjs(start);
  const e = end ? dayjs(end) : dayjs();
  if (!s.isValid() || !e.isValid()) return '—';
  return fmtDuration(e.diff(s, 'second'));
}
