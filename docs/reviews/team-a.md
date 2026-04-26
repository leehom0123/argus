# Team A "Guardrails" — Code Review

Independent reviewer pass. Scope: 9 commits on `feat/team-a-guardrails`
vs `main` (+3118 / -11 across 32 files). i18n parity test passes (960+
keys per locale, verified against `backend/backend/i18n/messages.py` +
`frontend/src/i18n/locales/{en-US,zh-CN}.ts`).

## TL;DR — APPROVE_WITH_NITS

Four tickets land cleanly (#12, #13, #33, #34) with strong test coverage
(5 new files, ~1700 LoC incl. `test_team_a_extra.py`). No blocking
security issues. Several correctness nits around the idle detector —
fixable in follow-ups without reverting.

## Blocking issues

None.

## Non-blocking findings

### Correctness

1. **`_check_idle_jobs` re-queries identical snapshot set per job**
   (`backend/backend/notifications/watchdog.py:576-588`). Query filters
   on `batch_id` only, so N running jobs → N× redundant queries. AND
   the decision is batch-level GPU util applied to every job: if a batch
   shares a GPU across jobs and one stalls the dataloader, ALL peer
   jobs get the amber flag. Fix: hoist the query out of the loop, or
   tie snapshots to `job_id` when reporters supply it.

2. **`Job.status.in_(("running", "RUNNING", None))`**
   (`watchdog.py:562`) silently excludes NULL-status rows — SQL `IN`
   does not match NULL. Fix: `or_(Job.status.is_(None),
   Job.status.in_(("running","RUNNING")))`.

3. **Divergence detector under-scans on multi-job batches**. The scan
   pulls `limit(50)` events per batch (`watchdog.py:753`); with 5 running
   jobs × `divergence_window ≥ 5` each job may not have enough
   `job_epoch` rows in the slice. Recommend per-job `job_epoch` query
   or bumping the fetch limit.

4. **Idle flag is sticky**: once `is_idle_flagged=True`, nothing clears
   it on recovery. Restart a job and the badge persists. Add an unflag
   path when util returns above threshold for the same window.

5. **Divergence race** on multi-worker deployments:
   `_check_batch_divergence` short-circuits on `status=="divergent"`, but
   two concurrent scans could both observe `"running"` and emit two
   `batch_diverged` events. Cheap fix: `UPDATE batch SET
   status='divergent' WHERE id=:id AND status != 'divergent'`, emit the
   event only when rowcount=1. Module docstring already admits
   single-worker assumption; noting for completeness.

### Security — GREEN

- **UA hash**: SHA-256 truncated to 16 hex chars; collision space is
  64 bits, bounded by the 50-entry cap → negligible risk.
- **`known_ips_json`**: ORM-only writes, capped at 50 entries, pruned to
  30 days (`backend/backend/api/auth.py:117,155`). IP is HTML-escaped on
  email render (`services/email.py` via `html.escape`). No SQL injection
  path.
- **Reporter-supplied `val_loss`**: `float(vl)` wrapped in
  `try/except (TypeError, ValueError)` at `watchdog.py:446`. No 500
  path from bad floats.
- **Malformed `known_ips_json`**: wrapped `try/except Exception → []`
  (`auth.py:110`). DoS via 10k logins is bounded by the 50-entry cap.
- **Backup endpoint** `/api/admin/backup-status`: `require_admin`
  gated, read-only, path confined to `BACKEND_DIR / "data" / "backups"`
  (`backend/backend/api/admin.py`). No traversal surface.
- **Backup cron filenames** use minute granularity
  (`app.py:206` → `monitor-YYYYMMDD-HHMM.db`); same-minute collisions
  are prevented by `interval_h * 3600`. SQLite backup uses the official
  `sqlite3.Connection.backup` API, safe against concurrent writers.

### Migration 016 — LGTM

- `is_idle_flagged` `server_default=sa.text("0")` correctly backfills
  existing rows (`016_guardrails.py:35`).
- `known_ips_json` nullable — existing users get `None`, first login
  enters the `not had_history` branch and stays silent. Correct.
- `downgrade()` drops in reverse with `batch_alter_table`. LGTM.

### UI

- `StatusTag.vue`: divergent uses `color="warning"` AND uppercase label
  text, so color-blind users still get the signal. Good.
- `JobIdleBadge.vue`: no explicit `aria-label`. `a-tooltip` emits
  `aria-describedby`, so screen readers announce the text on focus —
  acceptable. Nice-to-have: `aria-label="Idle job"` on `<a-tag>`.
- `Backups.vue`: empty-state + loading state present; error handling
  relies on the global toast interceptor (no inline error banner).
  Minor.

### Code-quality nits

- **Comment/default mismatch**: `config.py` says
  `alerts_anomalous_login_enabled` "Default off in tests; flip to True
  in prod" — default is already `True`. Update the comment.
- **Latent formatter bug**: `guardrails.batch.diverged.body` uses
  `{ratio:.1f}` but `_mark_divergent` passes `ratio=None` on the NaN/Inf
  branch (`watchdog.py:490`). No current caller renders the body, so
  latent — add a `reason=nan_or_inf` guard when wiring the in-app bell.
- i18n: all new user-facing messages route through `tr()` with en/zh
  parity.
- Config naming: `MONITOR_*` prefix consistent.
- Bare excepts annotated `# noqa: BLE001` with debug/error logging.

### Tests — missing edge cases worth adding

- Idle detector with `Job.status=NULL` (finding #2).
- Concurrent scan double-emit (finding #5).
- `_sqlite_path_from_url` with URL-encoded paths containing spaces.

## Scope creep

None. All 9 commits stay within the advertised four tickets. `f010fb8`
is a genuine fix: the watchdog was writing `is_idle_flagged` but
`_job_to_out` never copied it — the FE badge would have been dead on
arrival on main.

## Recommended follow-ups (non-blocking)

- `watchdog.py:562` — fix NULL-status job exclusion.
- `watchdog.py:576-588` — hoist snapshot query out of per-job loop.
- `watchdog.py:490` — guard `ratio=None` against `{ratio:.1f}`.
- `config.py` — correct `alerts_anomalous_login_enabled` doc comment.
- Add unflag path for `is_idle_flagged` on util recovery.
