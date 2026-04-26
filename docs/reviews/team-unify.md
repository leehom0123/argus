# Team Unify — Combined QA + Review

Branch: `feat/team-unify-public` (HEAD: `5fd0a79`)
Reviewer: combined QA + review pass, 2026-04-24.

## QA Section

### Backend test suite
```
603 passed, 5 failed, 1 skipped, 2 xfailed, 39 warnings in 150.59s
```
All 5 failures are pre-existing `test_artifacts.py` fixture issues unrelated to this
branch (storage-root setup, not auth/visibility). No regressions from the unify work.
New suites `test_anonymous_visibility.py` (31) + `test_demo_visibility.py` (13) pass.

### Frontend
- `pnpm install --frozen-lockfile` — clean
- `pnpm build` — succeeds, 29.34s, no TS errors
- `pnpm test:i18n` — i18n parity OK (1017 keys × 2 locales), 4/4 tests green

### Grep verification
| Pattern | Result |
|---|---|
| `PublicProjectList\|PublicProjectDetail\|PublicBatch` in `frontend/src/` | Only type names in `api/public.ts` + route `name: 'PublicBatch'` in `router/index.ts`. **Zero references to deleted page components.** |
| `hide_demo` in `frontend/src/` | Only `types.ts` + `api/auth.ts` as deprecated docstring/field (backwards compat). Correct. |
| `show_demo_toggle\|show_demo_description` | 0 matches. Correct. |

## Review Section

Security rating: **GREEN** for the demo-visibility flip itself; **YELLOW** for one
secondary surface (stars).

### Q1 — Star leak (yellow)
`backend/api/stars.py::list_stars` filters only `UserStar.user_id == user.id` — no
demo join. **Yes, demo rows leak** if a user starred a demo batch before the flip.
Practical impact is small: payload is `(target_type, target_id, starred_at)` only,
no batch metadata; detail lookups will 404 post-flip. Still worth a follow-up patch
(either server-side filter via `_demo_project_names` OR one-shot migration that
deletes pre-existing demo stars). Not a merge blocker — starring is private and
post-flip starring attempts require the batch to be visible first.

### Q2 — `_demo_host_names` perf (green)
Implementation at `backend/services/dashboard.py:764-804` is two DISTINCT queries
(demo-host candidates, then overlap with non-demo), both index-friendly (`batch.project`,
`batch.host`). **Not per-host iteration.** Safe at 100+ hosts — scales with distinct
host cardinality, not total hosts. No flag needed.

### Q3 — UI gate coverage (green)
`BatchDetail.vue` spot-check:
- **Rerun**: line 409-426, wrapped in `<template v-if="canWrite">` cluster with
  Star/Pin/Export/Share. OK.
- **Stop**: line 578, `<div v-if="canWrite">` wraps the popconfirm + button. OK.
- **Star**: line 410, `<StarButton>` inside the `v-if="canWrite"` cluster. OK.

`usePermissions.ts` logic is correct: explicit `readOnly` prop wins (so a logged-in
user previewing `/demo/*` gets read-only), else falls back to `!isAuthenticated`.

### Q4 — Dead code in `frontend/src/api/public.ts`
Still called (4): `createPublicShare`, `revokePublicShare`, `listBatchPublicShares`,
`listMyPublicShares` — used by `ShareDialog.vue` + `settings/Shares.vue` for
per-batch public-slug management. **Keep.**

Orphan (11): `getPublicBatch`, `listPublicJobs`, `getPublicJob`, `getPublicJobEpochs`,
`listPublicProjects`, `getPublicProject`, `getPublicProjectLeaderboard`,
`getPublicProjectMatrix`, `getPublicProjectActiveBatches`, `getPublicProjectResources`,
`getPublicProjectBatches`. These were the backing API calls for the now-deleted
`Public*` page components; router /demo/* and /public/:slug now reuse the internal
components with `readOnly=true`, which hit the authed endpoints. Safe to delete these
11 functions + their Public* interfaces — **nit, not blocker**.

### Blocking issues
None.

### Nits
1. Orphan public.ts functions (11) — follow-up PR: drop dead code, shrink bundle.
2. `list_stars` should filter demo (defence-in-depth + cleanup of stale rows).
3. `router/index.ts` route `name: 'PublicBatch'` is fine but inconsistent with the
   kebab-case `public-batch` / `public-project` names elsewhere. Cosmetic.

## TL;DR Merge Readiness

**GREEN** — backend + frontend green, demo-flip logic correct, UI gates cover write
paths, no blocking issues. Ship it; follow-up ticket for stars filter + dead-code
cleanup.
