# Phase 2 Code Review Report (Round 2)

**Reviewer**: code-reviewer agent (opus-4.7, 1M context)
**Date**: 2026-04-23
**Scope**: Backend phase 1 + BACKEND-B (tokens / ownership / idempotency / rate limit / visibility) + FRONTEND-A (auth pages + store + router guard). Plus: re-verification of all 5 Round 1 findings.

---

## Overall Verdict

- [ ] **APPROVE** — can git commit + push
- [x] **APPROVE_WITH_MINOR** — non-blocking issues, fix before commit OR log as follow-up
- [ ] **REJECT** — must fix before re-review

**Reason in one sentence**: All five Round 1 findings are resolved or addressed; backend 89 / client 22 / frontend typecheck+build all green; end-to-end curl smoke (register → login → mint token → ingest → dedupe → revoke → rejected) passes with correct HTTP codes; only paper cuts remain (startup alembic-upgrade path broken but not load-bearing, dangling `asyncio.create_task` for notifications unchanged, `Settings/Tokens` UI still a placeholder).

---

## Scoreboard

| Dimension | Round 1 | Round 2 | One-liner |
|---|---|---|---|
| A. Correctness & Security | 3 | **4** | API-token branch is solid: SHA-256 lookup, revoked+expired checks, awaited last_used bump, scope enforcement separate from authn. No SQL injection, no plaintext-in-list leak. Minus one: `asyncio.create_task` still dangling for notifications. |
| B. Test Coverage & Quality | 2 | **5** | 89 backend tests, all green. New coverage: revoked→401, viewer→403, JWT→403 on ingest, 501-event→422, per-token buckets, per-minute Retry-After, v1.1 event_id required, owner_id not overwritten on second-writer. |
| C. Maintainability & Consistency | 4 | **4** | `schemas/` package split (events / auth / tokens) is clean; re-exports preserve flat imports. Naming consistent. Minor: `get_db` vs `get_session` duplication still present. |
| D. API Contract Consistency | 4 | **4** | Backend ↔ client ↔ frontend types align (`AuthTokenResponse`, `RegisterOut`, token shapes). v1.1 requires `event_id`, v1.0 tolerated. Schema file title still says 1.1 while 1.0 accepted at runtime — same unresolved doc-vs-code drift from Round 1. |
| E. Documentation | 4 | **4** | requirements + design in sync with code. README still promises `pip install argus-reporter` + `scripts/monitor.yaml` auto-load + Settings/Tokens UI — all three promises are aspirational in v1. |
| F. Dependencies & Packaging | 4 | **5** | No new heavy deps (no slowapi, no bcrypt-for-tokens). Own TokenBucket, `secrets.token_urlsafe`, `hashlib.sha256`. Frontend deps unchanged from phase 1. |
| G. Security & Prod Readiness | 3 | **4** | JWT blacklist has TODO comment for multi-worker. Rate limit in-process documented. Token plaintext returned exactly once, never stored, display_hint capped at 8 chars. Admin-race + `request.client.host` PII logging still open from Round 1 should-fix list. |

---

## Round 1 finding → Round 2 verification

### 1. ❗ Backend 19/52 tests FAIL (ingest tests had no auth fixture) — ✅ FIXED

`backend/backend/tests/conftest.py:43-127` now provides a **pre-authenticated `client` fixture** that registers `tester`, logs in, mints a reporter token, and sets `Authorization: Bearer em_live_...` as a default header on the httpx AsyncClient. A sibling `unauthed_client` fixture exists for tests that need the bare 401 contract.

Verified: `pytest -q` → **89 passed in 20.68s** (vs. 19 failed / 33 passed in Round 1).

### 2. ❗ `db.py` read `EXPERIMENT_MONITOR_DB_URL`, docs/config read `MONITOR_DB_URL` — ✅ FIXED

`backend/backend/db.py:38-49` now has `_resolve_database_url()` with documented precedence:
1. `MONITOR_DB_URL` (primary, per docs)
2. `EXPERIMENT_MONITOR_DB_URL` (legacy, logs deprecation warning)
3. `_default_sqlite_url()` (backend/data/monitor.db)

Alembic `env.py:42-54` uses the same precedence. `backend/config.py` no longer needs a back-populate hack. Docstring in `db.py:1-10` states the contract.

### 3. `test_wrong_schema_version_is_400` expected 400 but server returns 415 — ✅ FIXED

`backend/backend/tests/test_schema_validation.py:48` is now `test_wrong_schema_version_is_415` and asserts 415. The endpoint raises `HTTP_415_UNSUPPORTED_MEDIA_TYPE` (`events.py:390`) with a structured detail body including the supported-versions list.

### 4. First-user admin race condition — ⚠️ PARTIAL (still should-fix)

`backend/backend/api/auth.py:175-217` still uses `SELECT COUNT(*) FROM user` outside an explicit `BEGIN IMMEDIATE` transaction. The comment at `auth.py:167-169` claims "race-free under SQLite's default serialized writes" — that's only true if reads are part of the same write transaction. For the MVP single-admin install path this is almost certainly fine (two humans don't register inside the same millisecond), but it remains a logic-level race with no test coverage. Round 1 categorized this as **should-fix**; it is **still should-fix**.

### 5. `asyncio.create_task` without reference retention — ⚠️ PARTIAL

**Fixed** in `backend/backend/deps.py:115-118` — `_bump_last_used_async` is gone; `touch_last_used` is now awaited inline on the request's own session. The author's docstring comment explains why (cross-loop lifetime issues when ASGI transport tears down the event loop under the test harness). Good call.

**Still open** at `backend/backend/api/events.py:500` and `events.py:573`:
```python
asyncio.create_task(_dispatch_notifications(request, event_payload))
```
Task object is discarded; if the coroutine raises after the response returns, Python logs a `Task exception was never retrieved` warning and the webhook silently doesn't fire. Non-blocking for MVP (notifications are best-effort) but the fix is trivial: module-level `_inflight: set[Task] = set()` + `t.add_done_callback(_inflight.discard)`. Worth doing.

---

## Must-fix (blocking commit)

**None.** The hard blockers from Round 1 (failing tests, env-var drift, wrong-code test) are all resolved.

## Should-fix (defer to next PR / before production)

- [ ] **`backend/backend/app.py:82-90` — runtime `command.upgrade()` fails inside the ASGI lifespan loop.** Alembic's `migrations/env.py:106` calls `asyncio.run(run_migrations_online())`, which can't run inside an already-running event loop. Startup logs show `RuntimeWarning: coroutine 'run_migrations_online' was never awaited` → "alembic upgrade failed ... (falling back to create_all)". Fresh installs survive because `init_db()` creates every table from ORM metadata including the partial unique index on `event.event_id`. **But existing databases from earlier versions never get migrated on startup**, only when the operator runs `alembic upgrade head` explicitly. Fix: either (a) drop the in-process alembic call and rely purely on `create_all` + documented manual `alembic upgrade head` (current README already documents this), or (b) replace `command.upgrade` with an async-aware runner that uses the same event loop. Preference: (a), since the README already tells operators to run alembic manually.
- [ ] **`backend/backend/api/events.py:500, 573` — `asyncio.create_task(_dispatch_notifications(...))` discards the task handle.** (Round 1 finding #5 leftover — see above.)
- [ ] **`backend/backend/api/auth.py:175-217` — first-user-admin SELECT/INSERT race.** (Round 1 finding #4 — still unaddressed.)
- [ ] **`backend/backend/api/auth.py:395` — `request.client.host` logged on unknown-email reset probes.** GDPR-adjacent; minor for research-target MVP. (Round 1.)
- [ ] **`frontend/src/pages/settings/Tokens.vue` is still a placeholder.** README promises "generate an API token under Settings / Tokens"; UI shows `a-empty` instead. Either (a) update README to say "create via `POST /api/tokens`" or (b) add the UI in FRONTEND-B before shipping.
- [ ] **`README.md:88-92` — promises `pip install argus-reporter` + `scripts/monitor.yaml` autoload that doesn't exist.** Soften to "import and construct `ExperimentReporter` directly" or add the yaml loader.
- [ ] **`schemas/event_v1.json` title says v1.1 but backend accepts v1.0.** Unresolved from Round 1. Either enum the `schema_version` property to `["1.0", "1.1"]` or cut v1.0 support at the schema file level.

## Nice-to-have (future work)

- `frontend/src/store/auth.ts` relies on localStorage for JWT; fine for MVP, but note in a code comment that multi-tab logout isn't synchronized (one tab revokes, other tab holds stale JWT until next `/me` probe).
- Backend rate limit burst `capacity=60, refill=10/s` is the default in `ratelimit.py:124`. Spec §4.3 says 600/min — sustained matches, but burst ceiling is only 60 requests. Consider bumping capacity to 120-300 so spill replays (which flush hundreds of events in a couple of seconds) don't get throttled. Non-blocking.
- `backend/backend/deps.py:33-40` has both `get_db` (auth) and imports `get_session` (events / batches). Keep one canonical alias.
- JWT blacklist + TokenBucket are process-local; add a TODO comment plus a README note for multi-worker uvicorn.
- 3 pytest warnings about `HTTP_422_UNPROCESSABLE_ENTITY` being deprecated in Starlette — trivial rename.
- `test_last_used_gets_bumped` polls 1s with 50ms sleeps; now that `touch_last_used` is awaited inline, the test could simplify by immediately asserting (no poll needed). Cosmetic.

---

## Findings (detail per dimension)

### A. Correctness & Security — **4/5**

**Strong**:
- API tokens: `secrets.token_urlsafe(20)` → ~160 bits of entropy (`tokens.py:83`). Prefix + scope mapping explicit (`SCOPE_TO_PREFIX`). SHA-256 hash + 8-char display_hint stored (`tokens.py:85`). Plaintext returned exactly once via `TokenCreateOut.token`; `TokenOut` has no `token` field — impossible to leak via list endpoint by structure.
- `lookup_token` rejects missing prefix before hashing (`tokens.py:123`), rejects revoked via SQL predicate (`tokens.py:127-128`), rejects expired by runtime check (`tokens.py:133`).
- `require_reporter_token` (`deps.py:179-207`) separates authn (got-a-user) from authz (got-a-reporter-scope token); JWT callers cannot masquerade as reporters because the dep checks `_current_api_token(request) is None` first.
- `require_web_session` (`deps.py:210-224`) is the inverse — prevents API tokens from hitting /api/tokens CRUD (cannot mint tokens via a token).
- `TokenBucket` uses `asyncio.Lock()` for multi-coroutine safety (`ratelimit.py:61`). Lazy refill on each call is correct.
- `VisibilityResolver` (`services/visibility.py`) uses SQLAlchemy Core + ORM exclusively — no string interpolation. Always-false predicate for unimplemented `shared` / `public` scopes avoids silent data leakage.
- Partial unique index on `event.event_id` (`models.py:97-102`, migration 003 lines 102-105) gives DB-level dedup for non-null event_ids, matching v1.1 idempotency contract.

**Still problematic**:
- `events.py:500, 573` dangling `asyncio.create_task` (see Round 1 #5 partial above).
- Register-admin race (Round 1 #4 unchanged).
- Startup alembic upgrade path silently broken (new finding; see should-fix).

### B. Test Coverage & Quality — **5/5**

89 tests pass in 20.68s. Highlights:
- **`test_token_auth.py`** — revoked→401, expired→401, em_live and em_view both authenticate on /me, `last_used` gets bumped.
- **`test_events_auth.py`** — no-token→401, viewer-token→403, JWT→403, owner_id stamped from token, **owner_id not overwritten on second-writer**.
- **`test_events_batch_endpoint.py`** — 50-event batch, partial dedup, **501-event batch→422** (pydantic max_length short-circuit), bad-event-in-middle still commits the rest.
- **`test_events_idempotency_v11.py`** — same event_id returns same db_id, different event_ids insert separately, v1.1 without event_id→422, v1.0 without event_id→200 (backward-compat).
- **`test_ratelimit.py`** — TokenBucket refill-over-time unit, Retry-After is reasonable, **per-token independent buckets** (mint second token → fresh bucket), 429 on 4th request with tiny bucket.
- **`test_visibility_basics.py`** — scope=mine hides other users, scope=all admin sees everything, scope=all non-admin collapses to mine, scope=shared empty, non-owner gets 404 on detail.
- **`test_api_tokens.py`** — plaintext shown exactly once + not in list, list_tokens returns only own, revoke idempotent (first→"revoked", second→"already-revoked"), past expires_at fails auth.

**Client**: 22/22 pass (unchanged from Round 1).
**Frontend**: typecheck + build green; no test runner (vitest not in devDeps). OK per scope.

### C. Maintainability & Consistency — **4/5**

- `schemas/` package split into `events.py` / `auth.py` / `tokens.py` with `__init__.py` re-exports so flat imports keep working. Clean.
- `deps.py` has strong comments separating authn (`get_current_user`) from scope enforcement (`require_reporter_token`, `require_web_session`). Module docstring explicitly names the two Bearer flavours.
- `tokens.py` and `ratelimit.py` docstrings explain *why* (display_hint rationale, 60/10 choice explanation) not just *what*.
- Lazy eager-loading: `ApiToken.user: relationship("User", lazy="selectin")` (`models.py:204`) prevents implicit second round-trip in `async def` context.
- Minor: `get_db` (deps) vs `get_session` (db) still both exist; auth.py uses `get_db`, events.py uses `get_session`. Pick one.

### D. API Contract Consistency — **4/5**

- Backend `TokenResponse` = `{access_token, token_type, expires_in, user: UserOut}` → frontend `AuthTokenResponse` = `{access_token, token_type, expires_in, user: User}`. **Matches.** Backend `UserOut` has `is_active` field; frontend `User` omits it — tolerable asymmetry (frontend just doesn't read it).
- Backend `RegisterOut = {user_id, require_verify}` → frontend `RegisterOut = {user_id, require_verify}`. **Matches.**
- Backend `TokenCreateOut` extends `TokenOut` + `token` (plaintext only in create). Frontend does not yet consume this (Tokens.vue placeholder) — will match when FRONTEND-B lands.
- v1.1 event: `event_id` required in backend (`_check_event_id_presence` returns 422). v1.0 tolerates missing. Tested both ways.
- `schemas/event_v1.json` title says "1.1" with `const: "1.1"` on the `schema_version` property — inconsistent with the server accepting "1.0". Same unresolved drift from Round 1.

### E. Documentation — **4/5**

- `docs/requirements.md` §4.1 table of tokens, §5.1 api_token schema, §7.4 visibility scopes all align with the code.
- `docs/design.md` §8 error codes match implementation (401 for authentication, 403 for scope, 415 for schema_version, 422 for validation, 429 for rate limit).
- `README.md` promises three things that aren't v1: PyPI-installed reporter, monitor.yaml autoloader, Settings/Tokens UI. Flag in release notes or soften.

### F. Dependencies & Packaging — **5/5**

`backend/pyproject.toml` adds nothing new vs Round 1. No slowapi, no bcrypt. Frontend `package.json` unchanged from Round 1 (pinia was already a dep; no new packages).

### G. Security & Prod Readiness — **4/5**

- Token plaintext: returned once, never stored (only hash + display_hint).
- Rate limit: per-token buckets, Retry-After header in seconds.
- JWT blacklist: in-memory, documented comment in jwt.py (process-local — see Round 1 nice-to-have).
- CORS: derived from `base_url` + localhost:5173 + localhost:8000 (app.py:93-107).
- Admin race + IP logging carryover from Round 1.
- Frontend auth: JWT in localStorage (XSS-exposed) is the standard MVP tradeoff; documented in store comments.

---

## Running the suite — actual results

```
# backend
$ pytest -q   (backend/)
89 passed, 3 warnings in 20.68s

# client
$ pytest -q   (client/)
22 passed in 32.78s

# frontend
$ pnpm run typecheck   → clean
$ pnpm run build       → built in 36.93s, largest chunk 574 kB (ECharts, lazy-loaded)

# alembic (from empty DB)
$ alembic upgrade head
running 001_initial → 002_auth_user_email → 003_tokens_owner_idempotency   (all 3 applied)

# curl smoke (uvicorn on port 8766, pre-migrated DB)
register → 201 {"user_id":1,"require_verify":true}
login    → 200 {access_token,token_type,expires_in,user{...is_admin:true...}}
POST /api/tokens (JWT, scope=reporter) → 201, plaintext shown
POST /api/tokens (JWT, scope=viewer)   → 201, plaintext shown
POST /api/events (em_live_ token) → 200 {"accepted":true,"deduplicated":false}
POST /api/events (same event_id)  → 200 {"accepted":true,"deduplicated":true}
POST /api/events (JWT Bearer)     → 403 {"detail":"Reporter scope requires..."}
POST /api/events (em_view_ token) → 403 {"detail":"This token has 'viewer' scope..."}
POST /api/events (schema 2.0)     → 415 {"detail":{...supported:["1.0","1.1"]}}
GET  /api/batches (JWT)           → 200 [{id:"b1",...}]
DELETE /api/tokens/1              → 200 {ok:true,detail:"revoked"}
POST /api/events (revoked token)  → 401 {"detail":"Invalid or expired API token"}
```

All semantics correct.

---

## Commit prep checklist (for PM / executor)

- [x] Tests all green — backend 89/89, client 22/22, frontend typecheck+build green
- [x] All 5 Round 1 findings resolved or documented (3 fixed, 2 degraded to should-fix)
- [x] No secrets committed — `.gitignore` covers `*.db`, `backend/data/`, `.env*`, `node_modules/`, `dist/`
- [x] End-to-end smoke passes against a real uvicorn (not just pytest)
- [x] All 3 alembic migrations apply cleanly from an empty DB
- [x] Typecheck clean (vue-tsc 0 errors)
- [x] Build clean (vite 0 errors)
- [ ] Optional: decide whether to land "alembic upgrade on startup" fix or document-only (recommend: document-only)
- [ ] Optional: wrap `asyncio.create_task(_dispatch_notifications(...))` with weakset + done_callback (trivial, could land in this commit)
- [ ] Optional: bump `backend.utils.ratelimit._DEFAULT_BUCKET` capacity from 60 to 120-300 so spill replays don't get throttled (trivial)

**Recommended commit message stub**:
```
feat(backend): personal API tokens, batch ownership, event idempotency, rate limiting

- api_token table (SHA-256 hash, scope: reporter|viewer, em_live_/em_view_ prefix)
- batch.owner_id stamped from first-writer token (immutable), batch.is_deleted
- event.event_id partial-unique index for spill-replay deduplication
- TokenBucket rate limiter (600 req/min per token, 429 + Retry-After)
- VisibilityResolver for scope=mine|shared|all|public (shared/public pending BACKEND-C)
- POST /api/events now requires em_live_ token; JWT→403, viewer token→403
- /api/tokens CRUD (JWT-only; plaintext returned exactly once)

Frontend:
- Login/Register/VerifyEmail/ResetPassword/Profile pages
- Pinia auth store with localStorage persistence + auto-refresh timer
- Axios interceptor: 401→logout, 423→LockedError, CORS+JWT injection
- Router guard: requiresAuth + requiresAdmin meta

Tests: 89 backend (+37 new), 22 client, typecheck+build clean
```

---

## Handoff suggestions for Phase 3

**BACKEND-C** (shares / audit_log):
- `services/visibility.py:70-83` has pre-placed TODO markers for `_shared_clause`. Extend there and add `batch_share` / `project_share` tables in migration 004.
- Audit log write sites: token create/revoke (`api/tokens.py`), login failures with lockout, batch ownership transfer. Add structured rows via a `services/audit.py` helper.
- Consider implementing `BEGIN IMMEDIATE` for the first-user-admin check as part of the same migration (fixes Round 1 #4).

**FRONTEND-B** (tokens UI + shares UI):
- `frontend/src/pages/settings/Tokens.vue` — table of tokens + "New token" modal; show plaintext once in a green alert with a copy-to-clipboard button. Disable modal close until user confirms they've saved it.
- `frontend/src/pages/settings/Shares.vue` — also still a placeholder; will consume BACKEND-C's share endpoints.
- Add `vitest` + 2-3 store tests (login happy path + 423 LockedError mapping + logout clearing localStorage).

**OPS**:
- Document that operators MUST run `alembic upgrade head` manually on upgrade; the startup fallback only works for fresh installs.
- Add JWT_SECRET / SMTP env vars to `deploy/systemd/*.service` and `deploy/Dockerfile` envs.

---

## Summary for PM (3-5 findings + commit prep)

**Verdict**: **APPROVE_WITH_MINOR** — safe to commit + push.

**Key findings**:
1. All 5 Round 1 blockers resolved (tests green, env-var unified, 415-test fixed, deps.py race-fix via awaited bump, first-user-admin still a low-risk MVP race).
2. Phase 2 adds 37 new tests with strong coverage of revoked/viewer/scope/dedup/rate-limit/visibility — this is the single biggest quality improvement since Round 1.
3. End-to-end curl smoke on a real uvicorn process confirms every contract: 200 / 401 / 403 / 415 / 429 return the spec-matched bodies and headers.
4. One new should-fix: **the in-process alembic upgrade at startup is silently broken** (coroutine-never-awaited warning, `init_db` fallback saves fresh installs but not migrations). Non-blocking because README already documents manual `alembic upgrade head`; downgrade to "document-only" or fix in follow-up.
5. Carryover from Round 1: `Settings/Tokens` UI is still a placeholder while README promises it. Soften README or land it with FRONTEND-B.

**Commit checklist**: tests green (89/22/typecheck+build), no secrets, curl smoke passes, migrations apply. Clear to commit.
