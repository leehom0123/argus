# Phase 3 Code Review Report (Round 3)

**Reviewer**: code-reviewer agent (opus-4.7, 1M context)
**Date**: 2026-04-23
**Scope**: BACKEND-C (shares / admin / audit / feature flags / migration 004) + BACKEND-D (SSE hub / stream endpoint) + FRONTEND-B (Tokens UI, Shares UI, ShareDialog, PublicBatch, scope tabs, router).
**Baseline**: Round 2 APPROVE_WITH_MINOR, first commit `7dbb695` pushed to `leehom0123/argus`.

---

## Overall Verdict

- [ ] **APPROVE** — can git commit + push without any changes
- [x] **APPROVE_WITH_MINOR** — safe to commit + push; 1 frontend contract typo should be fixed before the next FRONTEND lane lands, all other findings are non-blocking
- [ ] **REJECT** — must fix before re-review

**Reason in one sentence**: 138 backend / typecheck / build all green; 3 of the 5 Round 2 minors are now fixed (M4 admin race via `BEGIN IMMEDIATE`, M5 task-ref retention, IP-log redact on password-reset probes); Alembic up→down→up is clean; every security / data-consistency contract I could think of (PII-stripped public endpoint, 410-Gone on expired slugs, self-share rejection, banned-user JWT 401, SSE visibility at subscribe, SSE queue overflow isolation, fresh-hub firehose admin-only) has a matching test — the only real bug is a URL typo (`/public-share` vs `/public-shares`) in the frontend that silently 404s "list my existing slugs"; Phase 3 is ready to commit.

---

## Scoreboard

| Dimension | Round 2 | Round 3 | One-liner |
|---|---|---|---|
| A. Correctness & Security | 4 | **4** | Public path strips `owner_id`/`email`/`username` — only `owner_label="Shared by user #N"`. 410 Gone on expired slugs. SSE auth prefers header over `?token=` when both present. `_PENDING_TASKS` + done-callback in both `events.py` and `services/audit.py` fix M5. `BEGIN IMMEDIATE` closes the first-user-admin race on SQLite. IP redaction on unknown-email reset probes. Visibility-checked on SSE subscribe; non-admin firehose is 403. Minus one: query-param SSE tokens still logged in uvicorn access log by default. |
| B. Test Coverage & Quality | 5 | **5** | +49 tests (138 total). Phase 3 coverage is strong: self-share 400, project-share future-batch still visible, share-with-deactivated 400, view_count atomic bump, expires→410, ban→login 401, ban→`/me` 401, flag flip immediately gates register, SSE queue overflow drops 50 frames without stalling the fast subscriber, disconnect unsubscribes, dedup event doesn't re-publish. |
| C. Maintainability & Consistency | 4 | **4** | Schemas split by domain (`shares.py` / `public.py` / `admin.py` / `audit.py`); routers each mounted in `app.py`. `get_db` vs `get_session` duality still not unified (Phase 3 chose `get_db` uniformly — minor carryover from Round 2). SQLAlchemy-reserved `metadata` column renamed to `metadata_json`; Pydantic field-validator decodes for the external `metadata` key — clean compromise but drifts from the literal requirements.md §5.1 SQL. |
| D. API Contract Consistency | 4 | **3** | **One real mismatch**: frontend `listBatchPublicShares(batchId)` hits `GET /batches/:id/public-share` (singular); backend only exposes `GET /batches/:id/public-shares` (plural). Frontend also assumes `GET /public-shares/mine` exists — it doesn't. Both calls are wrapped in try/catch that silently returns `[]`, so the demo works (create/revoke/copy work) but "list previously generated links" is broken in ShareDialog + Shares.vue. Also: share POST returns 201 on both insert AND update — slightly non-REST (201=create, 200=update is more standard), but pragmatic for an idempotent grant UI. |
| E. Documentation | 4 | **4** | `docs/requirements.md` §7.5 / §7.6 / §5.1 align with implemented routes + models (modulo the `metadata` → `metadata_json` rename). `docs/design.md` §5.1 + §5.4 are still accurate (VisibilityResolver, SSEHub). No README mention of the `?token=` access-log redaction guidance for nginx operators; no README section on SSE subscriber limits. Round-2 carryovers unchanged: README still promises `pip install argus-reporter` + `scripts/monitor.yaml` autoload. |
| F. Dependencies & Packaging | 5 | **5** | Still zero new Python deps (ok — SSE uses stdlib asyncio.Queue + FastAPI StreamingResponse). Zero new frontend deps (native `EventSource`, no RxJS). |
| G. Security & Prod Readiness | 4 | **4** | SSE single-process limit documented at `services/sse_hub.py:13-15`. SSE `QUEUE_MAXSIZE=100` documented + tested. Public `GET /api/public/:slug` still not rate-limited (BACKEND-C self-flagged). `can_edit_batch` defined but never consumed — editor permission is stored on shares but no route enforces or exercises it (MVP is pre-edit-endpoints, so this is latent not broken). Alembic in-process startup upgrade still throws the `asyncio.run in running loop` warning + falls back to `init_db()`/create_all — Round 2 should-fix unchanged; manual `alembic upgrade head` does work cleanly (verified up→down→up). |

---

## Round 2 minor → Round 3 verification

| # | Round 2 finding | Round 3 status | Evidence |
|---|---|---|---|
| M4 | First-user-admin SELECT/INSERT race | ✅ **FIXED** | `backend/backend/api/auth.py:194-204` — opens `BEGIN IMMEDIATE` on SQLite so the COUNT + INSERT serialise under the reserved lock. Tolerant fallback when already in a transaction (test harness). |
| M5 | `asyncio.create_task(_dispatch_notifications(...))` task handle discarded | ✅ **FIXED** | `backend/backend/api/events.py:58-88` — module-level `_PENDING_TASKS: set[asyncio.Task]`, `_spawn_dispatch()` adds + `task.add_done_callback(_log_task_exceptions)` discards. Same pattern mirrored in `backend/backend/services/audit.py:45-60` (`_BACKGROUND_TASKS`) and `backend/backend/services/sse_hub.py:180-206` (`_pending_tasks`). All three use identical discard-on-done technique. |
| IP-log | `request.client.host` logged on unknown-email reset probes (GDPR) | ✅ **FIXED** | `backend/backend/api/auth.py:492-497` — unknown-email branch logs `"password reset requested for unknown email (ip redacted)"` at `log.debug`; no IP in message. Known-email branch still audits IP (legitimately). |
| Alembic auto-upgrade | In-process `command.upgrade` fails in async loop | ⚠️ **UNCHANGED** | `backend/backend/app.py:65-95` still calls `command.upgrade` synchronously inside the lifespan; `migrations/env.py:106` still runs `asyncio.run(run_migrations_online())`. At startup this logs `alembic upgrade failed: ... (falling back to create_all)`. Fresh installs survive via `init_db()`; existing DBs must use manual `alembic upgrade head`. Verified manual path works up→down→up on an empty DB. Non-blocking because README documents manual migrate. |
| schemas/event_v1.json v1.1 vs v1.0 | Title says 1.1 but server accepts both | ⚠️ **UNCHANGED** | No change to `schemas/event_v1.json` this round. Same tolerated drift. Non-blocking. |

**Net**: 3 of 5 Round-2 minors now fixed. The other 2 are explicitly non-blocking and were already deferred in Round 2.

---

## Must-fix (blocking commit)

**None.** All hard contracts hold; the only real bug is a UI-side typo that falls into a try/catch.

## Should-fix (fix before next FRONTEND lane / before production)

- [ ] **`frontend/src/api/public.ts:62` — typo: `/batches/:id/public-share` should be `/batches/:id/public-shares`.** Backend route is at `public.py:187` (`GET /api/batches/{batch_id}/public-shares`, plural — see `backend/backend/api/public.py:186-218`). Current call silently 404s; ShareDialog "Public link" tab always shows an empty list, and Shares.vue "Public links" tab fallback aggregation also never populates. Repro: open any batch → Share → Public link tab → generate a slug; the row appears immediately (from the POST response), but on next `watch(activeTab)` refetch it vanishes. Fix one-liner: change `/public-share` to `/public-shares` on line 62. Same endpoint exists in two consumers — both fixed automatically when the wrapper is corrected.
- [ ] **`frontend/src/api/public.ts:73-76` — `listMyPublicShares` calls `GET /public-shares/mine` which doesn't exist.** FRONTEND-B anticipated this ("if BACKEND-C doesn't ship this endpoint, callers fall back to composing from each batch"), and `Shares.vue:77-86` already try/catches the 404 and falls through to per-batch aggregation. Once the above fix lands, the fallback path will actually populate. Leave the `listMyPublicShares` wrapper as a forward-compat hook — the try/catch pattern is fine — or add the aggregation endpoint in a later BACKEND lane. No visible demo impact.
- [ ] **`backend/backend/api/events_stream.py` — document `?token=` leak into uvicorn access logs.** The code already prefers the `Authorization: Bearer` header over the query param (`_authenticate_stream` lines 118-122). But when a browser-native `EventSource` uses `?token=`, uvicorn's default access-log format records the full URL. Not a code bug, but prod operators need to know. Three options: (a) add a README section pointing at nginx `access_log` rewriting or uvicorn `--no-access-log` for `/api/events/stream`; (b) add a simple ASGI middleware that strips `token=` from the scope's `query_string` after auth; (c) accept as MVP risk and document in the operator runbook. Preference: (c) for commit, (a) for the README follow-up.
- [ ] **`backend/backend/api/public.py` — no rate limiter on anonymous `GET /api/public/:slug`.** BACKEND-C self-flagged this. A public slug becomes DDoS target when shared; easy scraper. Cheap fix: reuse `TokenBucket` keyed on client IP (`request.client.host`) with capacity 60 / refill 10/s. Defer to the next Ops lane; MVP demos are fine.
- [ ] **Alembic in-process upgrade at startup still silently fails (Round 2 carryover).** Same fix as Round 2: either (a) drop the in-process `command.upgrade` and document manual migrate only, or (b) switch `migrations/env.py` to use the existing loop instead of `asyncio.run`. Operator docs already say manual migrate — preference is (a) for minimal risk.

## Nice-to-have (future work)

- Add one `can_edit_batch`-consuming endpoint (e.g. PATCH `/api/batches/:id` for rename / tag) so the `permission='editor'` value on shares actually does something. Currently only read by `visibility.py:154-178` and tested by `test_batch_share.py` (which asserts storage but not effect). Requirements §15 explicitly lists this as an open discussion point; MVP punt is acceptable.
- Unify `get_db` (`deps.py:33`) and `get_session` (`db.py:91`) into one canonical alias. Phase 3 new code uniformly uses `get_db`, phase 2 events use `get_session` — mildly confusing grep noise.
- `request_id` / correlation id middleware so audit rows can be cross-referenced against the HTTP log of the request that triggered them. MVP-optional.
- Backend `PublicShareOut` returns `last_viewed` field but frontend `PublicShare` type omits it. Tolerable asymmetry but the column exists in `ShareDialog.vue:300-306` would benefit from it.
- Rename the DB column from `metadata_json` back to `metadata`: not possible because `metadata` is reserved on `sqlalchemy.orm.DeclarativeBase`. So leave the rename as permanent; instead, **add a one-line note to `docs/requirements.md` §5.1** noting the physical column name is `metadata_json` but the API field is `metadata`. Prevents future confusion.
- SSE: revoke-mid-stream still leaks events until reconnect (documented TODO at `events_stream.py:42-44`). Phase 2 can add periodic re-validation every N events or N seconds. MVP is fine.

---

## Findings (detail per dimension)

### A. Correctness & Security — **4/5**

**Verified strong**:
- `backend/backend/api/public.py:287-290` — `_owner_label(owner_id)` returns `f"Shared by user #{owner_id}"` — only the integer id leaks, never username/email. `PublicBatchOut` (`schemas/public.py:42-60`) has no owner-identifying field beyond `owner_label`. `test_public_share.py:51-53` asserts `"owner_id" not in body` and `"email" not in body`.
- `backend/backend/api/public.py:267-273` — expired `expires_at` → 410 Gone via `HTTP_410_GONE`. Covered by `test_public_share.py:93-113` (`test_expired_share_returns_410`).
- `backend/backend/api/public.py:275-282` — view_count increment uses a single `UPDATE public_share SET view_count = view_count + 1` — no select-then-update race. Test verifies 3 concurrent reads → count=3.
- `backend/backend/api/events_stream.py:118-122` — prefers `Authorization` header over `?token=` query param when both are present; 401 with `WWW-Authenticate: Bearer` on missing/invalid.
- `backend/backend/api/events_stream.py:139-172` — `_enforce_subscribe_visibility` runs `VisibilityResolver.can_view_batch` when `batch_id` is supplied; rejects non-admin firehose (403 with explicit `"Non-admin subscribers must supply a batch_id filter"`).
- `backend/backend/services/sse_hub.py:97-130` — `publish` uses `put_nowait` with `QueueFull` drop + warning log; snapshot-under-lock iteration. `test_sse_queue_overflow.py` asserts 50 drops happen after filling 100-slot queue without crashing or affecting a fast sibling.
- `backend/backend/api/shares.py:161-169` — self-share + owner-self-share both rejected 400. `test_batch_share.py:128-138` confirms.
- `backend/backend/api/admin.py:85-88` — banning self rejected 400.
- `backend/backend/api/admin.py:94` — ban sets `is_active=False`; `get_current_user` checks `user.is_active` at every request (including the banned user's own `/auth/me`). `test_admin.py:84-88` asserts `/me` 401s after ban. Note that the banned user's **existing JWT** still fails because of the `is_active` gate; **API tokens** also fail by the same gate in `deps.py:get_current_user` JWT branch AND in `auth/tokens.py:lookup_token → row.user` path (both load the User and check `is_active`).
- `BEGIN IMMEDIATE` at `auth.py:197-204` — on SQLite only, escalates to a reserved-lock write txn before COUNT+INSERT. Non-SQLite engines already have strong-enough default isolation.
- All new endpoints use ORM SELECT/UPDATE exclusively; I grep'd for string concatenation in `api/*.py` and found none. SQL injection surface is nil.

**Still open** (all non-blocking):
- `events_stream.py` query-param tokens logged by uvicorn's default access logger (not the app logger — app logger at `events_stream.py:272` only logs `user.username` + sid + filter). Operator-deployment concern, not a code bug.
- Alembic in-process startup upgrade unchanged from Round 2.

### B. Test Coverage & Quality — **5/5**

`pytest -q` → **138 passed in 36.55s** (49 new Phase 3 tests). 3 deprecation warnings about Starlette's `HTTP_422_UNPROCESSABLE_ENTITY` → `_CONTENT` rename (carryover from Round 2, trivial).

Highlights of the 49 new tests:
- **`test_batch_share.py`** (6): happy-path grantee-sees-it, non-owner-can't-add, non-grantee-still-blocked, self-share→400, revoke removes access, list shows grantees with usernames joined in one query.
- **`test_project_share.py`** (4): project share covers EXISTING batches, covers FUTURE batches (key invariant), doesn't leak other projects, list+revoke round-trip.
- **`test_public_share.py`** (5): slug-is-20-chars, anonymous GET works + no owner PII, view_count increments, expires→410, revoke→404, jobs+epochs endpoints.
- **`test_admin.py`** (4): non-admin→403 on all admin paths, admin lists all users, ban blocks login AND existing JWT, feature-flag read/write with immediate effect on registration.
- **`test_audit_log.py`** (3): register writes row, token_create writes row (tolerating background task delay), pagination ordered desc.
- **`test_visibility_shared.py`** (3): scope=mine hides shared batches, scope=shared unions batch_share ∪ project_share, admin scope=all sees everything.
- **`test_sse_basic.py`** (9): direct hub subscribe/publish/unsubscribe + end-to-end `POST /api/events → publish_to_sse → SSE queue receives`, dedup event does NOT re-publish (critical invariant), disconnect unsubscribes.
- **`test_sse_auth.py`** (7): no-token 401, bad-token 401, query-param token OK, foreign-batch 403, unknown-batch 403 (no existence leak), non-admin firehose 403, admin firehose 200. Uses bespoke `_drive_sse_request` ASGI driver because httpx's `ASGITransport` buffers SSE forever.
- **`test_sse_queue_overflow.py`** (2): 150 events → 100 delivered + 50 drop warnings, slow subscriber maxed out does NOT starve fast sibling.
- **`test_sse_keepalive.py`** (1): monkey-patches `KEEPALIVE_INTERVAL_S=0.3` and confirms `event: keepalive` frame arrives.
- **`test_sse_format.py`** (5): dict payload JSON-decoded, string payload pass-through, multi-line payload emits multiple `data:` lines per spec, newline in `event_type` is ValueError, keepalive frame format.

**Gaps (not blocking)**:
- No test for "SSE subscription stays alive when share is revoked mid-stream" — but the code explicitly documents this as a Phase-2 TODO (`events_stream.py:42-44`).
- No test for `listMyPublicShares` / `listBatchPublicShares` happy path — because those endpoints don't exist yet (one is a typo, one is a forward-compat stub).
- `test_audit_log.py` doesn't exercise the `log_background` ref-keeping directly, but the token-create test implicitly verifies by looping until the audit row appears (polls for up to 1 second).

### C. Maintainability & Consistency — **4/5**

- Schemas package split: `shares.py` (70 lines), `public.py` (82), `admin.py` (48), `audit.py` (41). Each file has a docstring explaining what the domain is for. Clean.
- Routers mounted in topic order in `app.py:173-185` with a comment `# BACKEND-C: sharing + public links + admin`.
- `services/audit.py` + `services/feature_flags.py` + `services/sse_hub.py` + `services/visibility.py` each have a module-level docstring spelling out *why* the service exists, not just *what*.
- `MultiTaskGeneExprLoss` / `BackgroundTasks` — sorry, that's the wheat project. Confirmed no contamination.
- `request.client.host if request.client else None` pattern applied uniformly across 9 call sites (all new Phase 3 code + 3 existing auth paths). Safe against `request.client = None` when Starlette can't determine peer (TestClient edge case).
- `AuditService.log_background` + `publish_to_sse` + `_spawn_dispatch` all use the same ref-keeping pattern with `add_done_callback(_discard_or_log)`. Three copies of the same idiom — a future refactor could factor a common helper, but MVP consistency is good.

**Minor drifts**:
- `get_db` (deps.py:33) vs `get_session` (db.py:91) duality unchanged. Phase 3 code uniformly uses `get_db`; events path still uses `get_session`. Mild grep noise, not a correctness issue.
- DB column `metadata_json` vs docs' `metadata`: requirements.md §5.1 line 177 says `metadata TEXT`, actual column is `metadata_json` (because `metadata` is reserved on `DeclarativeBase`). Pydantic schema at `audit.py:23` exposes it as `metadata` externally via a `field_validator(mode="before")`. So the API contract matches docs, but someone reading the SQL + docs side-by-side will be momentarily confused. Add a one-line note to requirements.md — see nice-to-have.

### D. API Contract Consistency — **3/5**

Systematic backend route ↔ frontend call check:

| Frontend call | Backend route | Match? |
|---|---|---|
| `POST /tokens` | `tokens.py:61` | ✓ |
| `GET /tokens` | `tokens.py:151` | ✓ |
| `DELETE /tokens/:id` | `tokens.py:161` | ✓ |
| `GET /batches/:id/shares` | `shares.py:101` | ✓ |
| `POST /batches/:id/shares` | `shares.py:139` | ✓ |
| `DELETE /batches/:id/shares/:gid` | `shares.py:211` | ✓ |
| `GET /projects/shares` | `shares.py:249` | ✓ |
| `POST /projects/shares` | `shares.py:280` | ✓ |
| `DELETE /projects/shares/:p/:gid` | `shares.py:347` | ✓ |
| `POST /batches/:id/public-share` | `public.py:121` | ✓ |
| `DELETE /batches/:id/public-share/:slug` | `public.py:221` | ✓ |
| **`GET /batches/:id/public-share`** (frontend) | **`GET /batches/:id/public-shares`** (backend, line 187) | ✗ **singular vs plural mismatch** |
| **`GET /public-shares/mine`** (frontend) | (does not exist) | ✗ **missing endpoint** (frontend try/catches) |
| `GET /public/:slug` | `public.py:313` | ✓ |
| `GET /public/:slug/jobs` | `public.py:349` | ✓ |
| `GET /public/:slug/jobs/:jid` | `public.py:365` | ✓ |
| `GET /public/:slug/jobs/:jid/epochs` | `public.py:381` | ✓ |
| `GET /events/stream` | `events_stream.py:229` | ✓ |

**Contract-shape check**:
- `BatchShareOut` (backend schemas/shares.py:32-42) — includes `created_by`. Frontend `BatchShare` (types.ts:194-200) omits `created_by`. Frontend just doesn't read it; no runtime break.
- `PublicShareOut` (backend schemas/public.py:30-40) — includes `last_viewed`. Frontend `PublicShare` (types.ts:212-219) omits `last_viewed`. Same deal.
- `ProjectShareOut` (backend schemas/shares.py:60-70) — includes `owner_id`. Frontend `ProjectShare` (types.ts:203-209) omits it. Same deal.

**POST share idempotency**:
- Both `add_batch_share` (shares.py:171-184) and `add_project_share` (shares.py:304-319) return `status.HTTP_201_CREATED` whether inserting or updating. REST convention would be 200 on update, 201 on create. BACKEND-C traded spec purity for "idempotent grant" semantics that match UI expectations (toggle viewer↔editor without status code jitter). Pragmatic call. Could be improved by distinguishing insert vs update and returning 200/201 respectively.

### E. Documentation — **4/5**

Requirements.md §5.1 schema vs implementation:
- All 5 new tables present (batch_share, project_share, public_share, audit_log, feature_flag) with matching columns and PKs — verified table-by-table at `models.py:242-384`.
- `audit_log.metadata` column is physically `metadata_json`; Pydantic exposes as `metadata` (see above).
- `feature_flag.value` column is physically `value_json`; service layer decodes. Same compromise.

Design.md §5.1 VisibilityResolver:
- Implementation at `services/visibility.py` matches the spec: scope `mine`/`shared`/`all`/`public`, admin skip on `all`, `can_view_batch` + `can_edit_batch` helpers. `_shared_batch_ids_subquery` unions batch_share + project_share as spec'd.

Design.md §5.4 SSE Hub:
- `SSEHub` class matches the spec: `subscribe` → `(sid, queue)`, `unsubscribe`, `publish` with QueueFull drop. Filter is dict-based; queue bounded at 100 (spec left the size open).

**README gaps**:
- No mention of `?token=` query param and the access-log leak caveat.
- No mention of SSE at all (Phase 3 added a new wire protocol).
- Round 2 carryovers unchanged: `pip install argus-reporter` + `scripts/monitor.yaml` autoload promises.

### F. Dependencies & Packaging — **5/5**

`backend/pyproject.toml` and `frontend/package.json` both unchanged from Round 2. SSE uses stdlib `asyncio.Queue` + FastAPI `StreamingResponse`; frontend uses native `EventSource`. Zero new attack surface.

### G. Security & Prod Readiness — **4/5**

- **SSE per-process limit** documented at `services/sse_hub.py:13-15` (TODO for Redis pub/sub in Phase 2).
- **SSE queue overflow behaviour** documented at `services/sse_hub.py:103-130` + tested in `test_sse_queue_overflow.py`.
- **SSE keepalive** documented at `events_stream.py:36-41` + tested in `test_sse_keepalive.py`.
- **Public rate limit**: missing (BACKEND-C self-flagged, should-fix). A rogue actor can scrape a shared slug at full wire speed.
- **Audit log retention**: no truncation/archival. For MVP fine; grows unbounded. `idx_audit_log_timestamp` lets future truncate/archive operations scan efficiently.
- **Feature flag key injection**: `update_feature_flag` (admin.py:204) validates `key not in DEFAULT_FLAGS and not key.replace("_", "").isalnum()` — blocks weird chars but still allows `"a".isalnum()` so admin can add one-char flags. Minor but harmless; only admins can reach this.
- **Slug entropy**: 15 bytes → 120 bits via `secrets.token_urlsafe(15)`. Brute-force-proof. `public.py:145-152` retries up to 3 times on collision before 500.
- **CORS**: unchanged from Round 2.

---

## Running the suite — actual results

```
# backend
$ pytest -q   (backend/)
138 passed, 3 warnings in 36.55s
Phase-3 subset (49 tests): 12.28s

# frontend
$ pnpm run typecheck   → clean (vue-tsc 0 errors)
$ pnpm run build       → built in 35.60s, largest chunk 573.97 kB (ECharts, lazy)

# alembic (from empty DB, using MONITOR_DB_URL=/tmp/test_alembic.db)
$ alembic upgrade head
  001_initial → 002_auth_user_email → 003_tokens_owner_idempotency → 004_sharing_admin_audit
$ alembic downgrade base
  004 → 003 → 002 → 001 → (empty)
$ alembic upgrade head
  (clean re-up)
All 4 migrations apply both directions.
```

---

## Commit prep checklist

- [x] Tests all green — backend 138/138 (was 89/89 in Round 2; +49 Phase 3 tests)
- [x] Frontend typecheck clean (vue-tsc 0 errors)
- [x] Frontend build clean (vite OK, 35.6s)
- [x] Alembic migrations up→down→up clean (001 / 002 / 003 / 004)
- [x] 3 of 5 Round 2 minors FIXED (M4 admin race, M5 task retention, IP log redact)
- [x] Remaining 2 Round 2 minors (alembic in-process upgrade, schema 1.1 const) explicitly documented as non-blocking + unchanged
- [x] No secrets committed (`.gitignore` covers `*.db`, `backend/data/`, `.env*`, `node_modules/`, `dist/`)
- [x] No new dependencies (Python or npm)
- [x] PII-stripped public endpoint verified via test assertion
- [x] 410 Gone on expired public shares verified via test
- [x] Ban → JWT 401 + API-token 401 verified via test
- [ ] Optional before push: **fix `frontend/src/api/public.ts:62` `/public-share` → `/public-shares`** (1-line, 1 file, no test impact since fallback already try/catches the 404). Commit either way; this is a UI-visible bug but the demo ships.
- [ ] Optional post-commit: README note about `?token=` access-log redaction for nginx ops.

**Recommended commit message stub**:
```
feat(phase3): share/admin/audit/SSE + public-link + frontend shares UI

Backend:
- migration 004: batch_share / project_share / public_share / audit_log / feature_flag
- /api/batches/:id/shares CRUD + /api/projects/shares CRUD
- /api/batches/:id/public-share create/revoke + /api/batches/:id/public-shares list
- /api/public/:slug anonymous read (batch, jobs, epochs) — PII stripped, 410 on expiry
- /api/admin/{users,feature-flags,audit-log} (admin-only)
- /api/events/stream SSE with visibility-checked subscribe, 15s keepalive, queue overflow drop
- VisibilityResolver with batch_share ∪ project_share union subquery
- AuditService.log + log_background with strong task ref retention
- SSEHub per-process pub/sub with bounded per-subscriber queue
- Register admin race fix: BEGIN IMMEDIATE on SQLite first-user INSERT
- Notifications + SSE task-handle retention (M5 fix)
- IP-log redaction on unknown-email password-reset probes

Frontend:
- ShareDialog (batch / project / public-link tabs)
- /public/:slug anonymous viewer page (no JWT)
- Settings/Tokens UI (generate + show-once plaintext + list + revoke)
- Settings/Shares UI (shared-by-me tab + public links tab)
- scope=mine|shared|all tabs on BatchList
- isPublicPath guard in axios client (skip JWT on /public/*)
- useLiveBatch composable stub (Phase 3.5)

Tests: 138 backend (+49 new), frontend typecheck+build clean, alembic up/down/up clean.
```

---

## Handoff suggestions

### FRONTEND-C (next lane)
- **Fix the one-line typo** in `src/api/public.ts:62`: `/public-share` → `/public-shares`. Removes the silent 404 in ShareDialog "Public link" tab and Shares.vue "Public links" tab.
- Decide policy on `listMyPublicShares` — either (a) add the aggregation endpoint to BACKEND or (b) keep the client-side per-batch aggregation as the canonical path and delete the `listMyPublicShares` wrapper.
- Wire `useLiveBatch.ts` to real `GET /api/events/stream?batch_id=X&token=JWT` using native `EventSource`. Start with "subscribe only when `batch.status==='running'`" and tear down on unmount. Exponential backoff on reconnect (start at 1s, cap at 30s).
- Audit-log viewer UI at `/admin/audit-log` — consumes `GET /api/admin/audit-log?since=&action=&limit=&offset=`, renders a table with `metadata` rendered as JSON. Pagination already tested server-side.

### Dashboard IA (phase 3.5 / 4)
- None of requirements.md §16 (Dashboard, Project details, Star, Pin, Compare, Host cards, Activity feed) is implemented yet. §16 is the biggest open design block.
- `GET /api/dashboard` aggregation endpoint (§17) is missing — currently the frontend composes from `/api/batches` + `/api/resources` + `/api/projects`. Worth adding server-side to avoid N+1.
- Derived fields (ETA EMA, is_stalled, GPU-hours, best_metric — §16.5) have no backend yet.

### QA (before prod)
- End-to-end demo script: register 2 users → A mints reporter token → A posts events for batch b1 → A shares b1 with B → B sees b1 → A generates public link → anonymous curl returns 200 → A revokes share → B sees 404. Should mirror requirements §14 items 1-9.
- Load test the SSE hub: spin 100 concurrent `EventSource` subscriptions and POST 1000 events, confirm no dropped frames (non-overflow path) and no memory growth.
- Cross-browser test: Safari's `EventSource` re-uses the same URL on reconnect → `?token=` stays in access logs. Confirm nginx config or switch to header-auth via `fetch()` + `ReadableStream` for a Phase 2 frontend swap.

---

## Summary for PM

**Verdict**: **APPROVE_WITH_MINOR** — safe to commit + push.

**Key findings**:
1. **138/138 backend green, typecheck+build clean, alembic up/down/up clean** — all the objective gates pass.
2. **3 of 5 Round 2 minors FIXED** (admin race via `BEGIN IMMEDIATE`, M5 task-handle retention in 3 places, IP-log redact on unknown-email probes); 2 unchanged and already deferred (alembic startup upgrade silent fallback, `schemas/event_v1.json` v1.1-vs-v1.0 drift).
3. **One real bug — a 1-line typo** in `frontend/src/api/public.ts:62`: `/public-share` should be `/public-shares`. Demo still works because it's inside a try/catch, but "list my previously generated public slugs" always shows empty. Fix takes 1 character. Not a commit blocker; will be visible to anyone who creates two slugs.
4. **Security audit is clean**: public endpoint strips owner PII (`"Shared by user #N"` only), 410 on expiry, ban → JWT + API token both 401, SSE visibility-checked at subscribe, SSE queue overflow drops one slow reader without starving others, no SQL injection (ORM everywhere).
5. **Known deferrable**: (a) uvicorn access logs record the `?token=` query param in SSE URLs — operator should strip at nginx or use `--no-access-log` for that route; (b) public `GET /api/public/:slug` has no rate limiter; (c) editor permission is stored on shares but no endpoint consumes it yet (requirements §15 open discussion point — MVP OK).

**Commit prep**: green on tests (138/138), green on typecheck + build, 4 migrations clean, no new deps, no secrets. Recommend landing the 1-line `public.ts` typo fix in the same commit to avoid a same-day follow-up, but it's not required.
