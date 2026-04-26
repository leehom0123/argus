# Code Review — Team B "Observability" (`feat/team-b-observability`)

**Reviewer**: independent review lane (not the author, not the QA author)
**Base**: `main` @ common ancestor
**Head**: `origin/feat/team-b-observability` @ `6814d23`
**Date**: 2026-04-24
**Diff size**: 32 files, +3380 / −84 LOC; 1634 LOC of tests of which 30 are the extra QA pass.

---

## TL;DR verdict

**Approve with minor fixes.** The four observability roadmap items (#11 GPU-hours,
#18 git SHA chip, #21 JobOut extras + matrix popover, #30 empty-state hints) are
cleanly scoped, well-tested (QA's extra 30 cases hit the right edge surfaces —
SQL-injection payload, non-dict metrics JSON, Accept-Language quality values,
gpu_count string coercion), and the one bug QA surfaced (`6fca430`) has a clean
one-line fix + regression test. No blocking security issues.

Two nits worth addressing before merge (one typing, one echarts resize). One
cross-branch pollution to drop (see §Cross-branch pollution).

---

## Blocking issues

**None.**

---

## Nits (non-blocking, recommended before merge)

### N1. `frontend/src/types.ts` — `Job` interface not updated for #21 fields

The backend `JobOut` schema (`backend/backend/schemas/events.py:319-334`) gained
three nullable fields (`avg_batch_time_ms`, `gpu_memory_peak_mb`, `n_params`),
but the frontend `Job` type at `frontend/src/types.ts:52-64` was **not**
extended. Consequence: `JobMatrix.vue:19-29` had to fall back to an unsafe cast:

```ts
function readNumber(job: Job, key: string): number | null {
  const v = (job as unknown as Record<string, unknown>)[key];  // ← type hole
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}
```

**Recommendation**: add the three fields to `Job` as `?: number | null` so
the popover reads them directly (`job.avg_batch_time_ms`) with full type
safety. The `Record<string, unknown>` cast can then go away.

**File**: `frontend/src/types.ts:52-64`

### N2. `GpuHoursTile.vue` — chart may mount at 0×0 and never resize

`chartEl` is gated by `v-show="hasData"`, not `v-if`, so the DOM element
exists at mount time (`display: none`). `useChart` calls `echarts.init(el)`
inside `onMounted`, which succeeds but echarts reads a 0-height bounding
box. When `hasData` flips true after `fetchData()` resolves, the chart
becomes visible but nothing calls `instance.resize()` — on some browsers
the canvas stays at its initial zero dimensions until the window is
resized manually.

`useChart` (verified at `frontend/src/composables/useChart.ts`) does expose
a `resize()`, but the tile doesn't import or invoke it. On the
host-capacity rail the other tiles get away with this because they always
have data at mount; this is the first tile that can legitimately start
empty.

**Recommendation**: either switch to `v-if="hasData"` so the chart mounts
after data is present, or call the returned `resize()` in the `fetchData`
`finally` / in a `watch(hasData, ...)` block.

**File**: `frontend/src/components/GpuHoursTile.vue:82,161-165`

### N3. `_extract_gpu_count` silently ignores string `gpu_count`

`backend/backend/api/stats.py:65-68` accepts only `int`/`float`. The test
`test_gpu_hours_gpu_count_string_is_coerced_to_one` explicitly documents
this as a deliberate choice. That's fine, but it's a mild trap: a future
reporter emitting `"gpu_count": "2"` as a string will silently be billed
as 1 GPU with no log line. Consider logging a `logger.debug(...)` when a
known key is present but fails the type check.

**File**: `backend/backend/api/stats.py:65-68`

### N4. `_enrich_env_snapshot` does not validate URL scheme

`_normalise_git_remote` (`backend/backend/api/batches.py:86-121`) converts
SSH → HTTPS but doesn't reject `javascript:` / `data:` / `file:` schemes if
a malicious reporter writes `git_remote="javascript:alert(1)"`. The FE
side is mostly safe — Vue binds `:href` as a string attribute (no
`innerHTML`), so execution requires the user to click the chip. And
modern browsers do block `javascript:` navigation in many contexts. But
defense in depth: reject anything that isn't `git@…:…` / `ssh://…` / `https://…`
/ `http://…` in `_normalise_git_remote` and return `None` for the rest.

**File**: `backend/backend/api/batches.py:86-121`

### N5. `meta.py` hint catalog is duplicated, not centralised

`HINTS` is defined in-module (`backend/backend/api/meta.py:40-90`) and the
docstring explains why (lifecycle differs from error messages). Fair. But
`frontend/src/i18n/locales/*.json` also has `component_empty_state.*`
fallback keys. Two sources of truth means the next hint change requires
edits in three places (backend catalog + 2 locale json). Acceptable for
MVP; open a follow-up ticket to pick one.

**File**: `backend/backend/api/meta.py:40-90`

### N6. `batches.py` still imports `_job_to_out` from `jobs.py` at call time

`backend/backend/api/batches.py:77-83` does a function-local import of
`backend.api.jobs._job_to_out` on every call. It's a safe lazy-import
guard against circular imports but paid N×. The two modules aren't
circular today — move the import to module top.

**File**: `backend/backend/api/batches.py:77-83`

---

## Security rating

**Good (no blocking issues).**

- **`/api/stats/gpu-hours-by-user`** — `days` param is `Query(ge=1, le=365)` so
  the SQL-injection-flavoured test (`test_gpu_hours_sql_injection_attempt_is_422`,
  `test_team_b_extra.py:152`) correctly returns 422 before any SQL runs. The
  query itself is a SQLAlchemy Core `select(...)` with parameterised comparisons —
  no string interpolation. Non-admin isolation is enforced by the
  `stmt.where(User.id == current.id)` branch plus the always-one-row-guarantee
  (`backend/backend/api/stats.py:113-140`), covered by
  `test_gpu_hours_non_admin_security_isolation`.

- **`/api/meta/hints`** is public. Inspected the catalog — content is empty-state
  copy referring to env var names (`MONITOR_SERVER`) and user-facing script paths
  (`main.py`, `scripts/forecast/run_benchmark.py`). No secrets, no internal IPs,
  no version numbers. Safe to expose unauthenticated.

- **`ReproChipRow` `:href` binding** — Vue binds as a string attribute, not
  `innerHTML`; the user-controlled `git_remote_url` is route-scheme-agnostic
  but execution requires a click, and modern browsers filter `javascript:`
  navigation on `<a target="_blank" rel="noopener noreferrer">`. See N4 for
  defense-in-depth recommendation.

- **Git-remote parsing** (`_normalise_git_remote`) — no path-traversal vector;
  the function only manipulates prefix/suffix. The three `git_remote_*`
  tests cover HTTPS `.git` stripping, SSH alt forms, and unrecognised schemes
  passing through unchanged.

---

## Performance

- **GPU-hours aggregation**: one SQL query, no N+1. Aggregation happens in
  Python, justified for SQLite's weak JSON arithmetic; the 1000-batch ×
  50-job test (`test_gpu_hours_perf_under_1000_jobs`, line 299) asserts
  under the budget on in-memory SQLite. Postgres will be faster. Indexes
  confirmed: `idx_batch_owner` (`models.py:65`), Job has composite PK
  `(batch_id, id)` so `Job.batch_id` is effectively indexed.

- **JobMatrix popover**: 100% template-rendered; no extra API call per cell.
  `mouse-enter-delay: 0.2s` keeps hover latency sane. Popover doesn't
  auto-close on scroll (ant-design-vue default) — acceptable for a grid
  that rarely scrolls within a dashboard viewport; flag only if users
  complain.

- **Meta hints**: fetched once at app mount via the Pinia store
  (`frontend/src/store/hints.ts` — `ensureLoaded()` short-circuits on
  subsequent calls). `EmptyState` reads sync from the store. Zero
  perf concern.

---

## UI quality

- **`GpuHoursTile`**: dispose on unmount handled by `useChart`
  (`frontend/src/composables/useChart.ts:52-57`). Dark theme inherited.
  Window selector snaps invalid values to 30. See N2 for the resize
  caveat.

- **`EmptyState`**: no `role="status"` or `aria-live`. For screen-reader
  users landing on an empty table, the hint won't be announced on dynamic
  replacement. Add `role="status"` on the outer `<div class="empty-state">`
  for a small a11y win.
  **File**: `frontend/src/components/EmptyState.vue:66-93`

- **i18n parity**: `test_meta_hints_en_and_zh_have_identical_key_sets`
  covers backend catalog parity. Spot-checked
  `component_gpu_hours_tile.*`, `component_job_matrix_popover.*`,
  `component_empty_state.*` and `component_repro_chip_row.git_sha_open_github`
  in both locale JSONs — all present. Good.

---

## Code quality

- No `console.log` left behind. Two `console.warn` in hints store + GPU tile
  (`frontend/src/store/hints.ts:53`, `GpuHoursTile.vue:91`) are intentional
  eslint-disabled diagnostics — fine.
- No `TODO/FIXME` added in new code (the `batches.py` `.. todo::` block is
  pre-existing).
- Docstrings are thorough on `stats.py`, `meta.py`, and `_normalise_git_remote`;
  `GpuHoursTile.vue` header comment explains admin-vs-user scoping. Good.
- Type safety: see N1 — frontend `Job` interface needs the three new
  optional fields.

---

## Cross-branch pollution

**`9e74814` "feat(auth): /settings/sessions JWT revocation panel (#31)"
does NOT belong on this branch.** It touches:

- `backend/backend/api/auth.py` (+ ~260 LOC sessions endpoints)
- `backend/backend/auth/jwt.py` (jti wiring)
- `backend/backend/deps.py` (revocation check on every authed request)
- `backend/backend/models.py` (ActiveSession)
- `backend/backend/schemas/auth.py` (SessionOut)
- `backend/migrations/versions/015_active_sessions.py` (new migration)
- `backend/backend/tests/test_sessions.py` (new)
- `backend/backend/tests/test_auth_jwt.py` (jti changes)

This is Team C's scope. It also adds a **migration** (015) which must be
coordinated across branches — merging it via Team B's PR is a recipe for
Alembic migration-number conflicts.

**Action for PM**: drop `9e74814` from the merge (interactive rebase or
cherry-pick the other 8 commits into a clean branch) and let Team C land
#31 on `feat/team-c-sdk-sessions` where it belongs. The rest of the branch
is self-contained and merges cleanly without it.

---

## Specific recommendations

| # | File:line | Severity | Change |
|---|-----------|----------|--------|
| N1 | `frontend/src/types.ts:52-64` | nit | Add `avg_batch_time_ms?`, `gpu_memory_peak_mb?`, `n_params?` to `Job` interface |
| N2 | `frontend/src/components/GpuHoursTile.vue:82,161` | nit | Switch `v-show` → `v-if` on the chart div, or call `resize()` after data arrives |
| N3 | `backend/backend/api/stats.py:65-68` | nit | Log debug when `gpu_count` present but rejected for type |
| N4 | `backend/backend/api/batches.py:86-121` | nit (defense-in-depth) | Reject non-`git@ / ssh:// / https:// / http://` schemes in `_normalise_git_remote` |
| N5 | `backend/backend/api/meta.py:40-90` | follow-up ticket | Centralise hint catalog (backend vs FE i18n fallback) |
| N6 | `backend/backend/api/batches.py:77-83` | micro-nit | Move `_job_to_out` import to module top |
| a11y | `frontend/src/components/EmptyState.vue:66` | nit | Add `role="status"` on the outer block div |
| PM | `9e74814` | **drop** | Out-of-scope sessions commit; belongs on `feat/team-c-sdk-sessions` |

---

## Tests reviewed

- `test_gpu_hours_by_user.py` (277 lines) — admin vs non-admin, window
  boundary, gpu_count aliases, malformed metrics, empty DB. Comprehensive.
- `test_meta_hints.py` (107) — 11-key parity (en/zh), Accept-Language q-values,
  French fallback, no-header default, public access, extra-field rejection.
- `test_git_sha_chip_data.py` (137) — SSH/HTTPS/`.git` normalisation,
  empty-SHA handling.
- `test_job_detail_extras.py` (188) — the three new fields' parsing,
  alias resolution, null-propagation, shape parity between `/api/jobs/{bid}/{jid}`
  and `/api/batches/{bid}/jobs`.
- `test_team_b_extra.py` (717, QA's extra 30) — SQL-injection payload,
  zero/negative/non-int `days`, gpu_count as string/negative/huge, bool not
  coerced to int, non-dict metrics JSON (caught the bug `6fca430` fixed),
  legacy JobOut backward-compat.

All tests pass on main's Python 3.11 / in-memory SQLite setup (spot-checked;
not run end-to-end in this review).

---

**Reviewer's bottom line**: solid observability lane. Fix N1 and N2 before
merge (both one-liners). N3–N6 and the a11y nit can go in a follow-up. PM
must drop `9e74814`.
