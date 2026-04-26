# Phase 1 Code Review Report

**Reviewer**: code-reviewer agent (opus-4.7, 1M context)
**Date**: 2026-04-23
**Scope**: schema v1.1, backend v1 + BACKEND-A, client v1.1, frontend v1, docs, README

---

## Overall Verdict

- [ ] **APPROVE** — can git commit + push
- [ ] **APPROVE_WITH_MINOR** — non-blocking issues, fix before commit
- [x] **REJECT** — must fix before re-review

**Reason in one sentence**: Backend test suite does not pass (19 / 52 failed) and two production-breaking bugs (`MONITOR_DB_URL` env var is silently ignored by the runtime engine; notification channel instances always carry `None` webhooks) ship in the committed code.

---

## Scoreboard

| Dimension | Score (1-5) | One-liner |
|---|---|---|
| A. Correctness & Security | 3 | Argon2id / JWT / token hashing are solid; two bugs (db-url drift, untested ingest path) are outright broken. |
| B. Test Coverage & Quality | 2 | Client: 22/22 green. Backend: **19/52 tests FAIL** — PM's "52 passed" claim is wrong. Frontend has no tests. |
| C. Maintainability & Consistency | 4 | Clear module boundaries, typed, docstring-heavy. Minor inconsistency between `get_db` and `get_session`. |
| D. API Contract Consistency | 4 | Schema v1.1 is applied consistently in client + backend + tests. Test schema_version=1.0 collides with the 415 return code. |
| E. Documentation | 4 | Requirements + design are thorough and in sync; README is clean and accurate. |
| F. Dependencies & Packaging | 4 | Clean pyproject / package.json; version bounds are reasonable; no secrets in repo. |
| G. Security & Prod Readiness | 3 | Prod safeguards exist (argon2id, constant-time 401, lockout, rate limit) but auth-exempt paths and email-IP-logging need minor hardening. |

---

## Must-fix (blocking commit)

- [ ] **`backend/backend/db.py:34-36` — runtime engine reads wrong env var.** `DATABASE_URL` is read from `EXPERIMENT_MONITOR_DB_URL`, but `Settings.db_url` (`backend/backend/config.py:49-53`) and all docs (`README.md:70`, `docs/requirements.md` §10.2, `docs/design.md` §10.2) use `MONITOR_DB_URL`. `config.py:104-105` back-populates `MONITOR_DB_URL` from `EXPERIMENT_MONITOR_DB_URL` but not the other direction. Consequence: a production operator who follows the README and sets only `MONITOR_DB_URL` ends up with **alembic upgrading one DB** (the one settings points to) while **runtime reads/writes another** (the default `backend/data/monitor.db`). The only reason tests don't catch this is because `conftest.py:19-22` sets both env vars. **Fix**: have `db.py` read `from backend.config import get_settings` and use `get_settings().db_url` once, or back-populate both directions in `config.py`.

- [ ] **Backend test suite: 19 of 52 tests FAIL.** Running `pytest -q` in `backend/`:
  ```
  19 failed, 33 passed in 9.55s
  ```
  Root cause: `backend/backend/api/events.py:484` gates `POST /api/events` behind `Depends(enforce_ingest_rate_limit)` → `require_reporter_token` → requires `Bearer em_live_*`. Every event-ingest test in `test_events_post.py`, `test_idempotency.py`, `test_schema_validation.py`, and `test_batches.py` POSTs events with no `Authorization` header, so every one returns 401 instead of the expected status. The PM's "52 passed" count is a false positive — someone counted without actually running the suite on the current tree.
  - **Fix**: add a `conftest` fixture that registers a user, mints an `em_live_` token via the direct helper in `backend/auth/tokens.py`, and injects `Authorization` on all event POSTs in those tests (or exposes a fixture `reporter_client`).

- [ ] **`backend/backend/api/events.py` — schema-version validation happens AFTER Pydantic envelope parse.** `EventIn.schema_version: str` accepts any string; `_check_schema_version` is only called inside the endpoint *after* dependency injection resolved. That means `test_schema_validation.py::test_wrong_schema_version_is_400` expects 400 but the code returns 415 (per `design.md §8`). One of the two is wrong; design says 415 is canonical. **Fix**: update the test to `assert r.status_code == 415` (not 400) so contract + implementation agree.

- [ ] **`backend/backend/app.py:40-57` — FeishuNotifier is instantiated with `None` when webhook is missing.** The guard is correct when `EXPERIMENT_MONITOR_FEISHU_WEBHOOK` env var is empty, but it silently swallows failures to parse the YAML file AND never treats `feishu_url = None` as a skip — the exception handler logs and moves on (`feishu_url` stays `None`), then the `if feishu_url and "REPLACE_ME" not in feishu_url` guard correctly does nothing. So actually this **works**, but the reverse case is broken: if the YAML has a well-formed `channels.feishu.webhook_url` but the env var also is set to a different value, the env wins and the YAML is never consulted (read the flow at line 42-55 — the fallback is only taken when the env var is unset). That is by documented design (env > yaml), so this is **not** a blocker — recategorising as should-fix. See Should-fix item.

- [ ] **`backend/backend/tests/conftest.py` — test teardown crashes with `sqlite3.ProgrammingError: Cannot operate on a closed database`.** Visible on isolated runs (see `pytest backend/tests/test_schema_validation.py::test_wrong_schema_version_is_400 -v`). The `AsyncClient`/`lifespan_context` call `dispose_db()` which closes the engine, but the per-test fixture reuses that same engine via `db_mod.engine`. Second test in a file either crashes or leaves dangling warnings. Fix: either recreate the engine per-test (costly) or skip `dispose_db()` in the lifespan for tests.

## Should-fix (defer to next PR)

- [ ] **`backend/backend/services/email.py:67` — `field(default_factory=list)` used inside `__init__`.** `dataclasses.field()` is a no-op when not inside a `@dataclass` class body. The very next line `self.sent_messages = []` shadows it, so functionally fine but confusing and emits a type error under strict mypy.
- [ ] **`backend/backend/api/auth.py:395` — `request.client.host` may leak to logs despite IP being PII under GDPR.** Logging IP for "unknown email" password-reset probes is reasonable for abuse prevention but should be hashed or truncated (`/24`) for the research tool target audience. Non-blocking.
- [ ] **`backend/backend/api/auth.py:175-217` — first-user-admin race condition.** `SELECT COUNT(*) FROM user` runs in read-mode, which under SQLite default isolation can be observed by two concurrent registrations before either commits; both then INSERT with `is_admin=True`. Comment at line 168 claims "race-free under SQLite's default serialized writes" — that's only true if writes are serialized, but reads happen before the write. For MVP acceptable; add a `UNIQUE WHERE is_admin=true` or explicit `BEGIN IMMEDIATE` in the next PR.
- [ ] **`backend/backend/api/events.py:500` — `asyncio.create_task` without reference retention.** Background notification tasks are un-awaited and un-tracked. If the task raises after the response goes out, Python logs a `Task exception was never retrieved` warning and the webhook silently doesn't fire. Keep a process-level weakset of in-flight tasks or use `BackgroundTasks`.
- [ ] **`backend/backend/deps.py:122-126` — `asyncio.create_task(_bump_last_used_async(row.id))` fired from every authenticated request.** Not awaited, not tracked, and opens a brand-new session per request. Under sustained ingest traffic this can create task pile-up. Accept "best-effort" is fine for MVP but bound the concurrency (semaphore) or debounce.
- [ ] **`backend/backend/db.py:34-36` (same issue) — once fixed, also drop the engine-level global and wrap it behind `get_settings()` so the test harness doesn't have to double-set env vars.**
- [ ] **`backend/backend/api/events.py:508` — `ingest_events_batch` commits per-event inside a `for` loop (line 536).** With 500 events that's 500 round-trips; comment at line 534 already acknowledges this. For MVP fine, but a perf footgun during spill replay.
- [ ] **`backend/backend/api/events.py:559-567` — batch item failure serializes `str(exc)` or `repr(exc)` into the response.** Harmless for the reporter client (which logs+drops), but could leak internal paths on RuntimeError. Wrap with a generic "internal error" + trace_id.
- [ ] **Inconsistency: `backend.deps.get_db` vs `backend.db.get_session`.** Both are the same generator; auth uses `get_db`, batches/jobs/events use `get_session`. Pick one and inline.
- [ ] **Empty directory `backend/backend/config/`.** Not packaged by setuptools find, so `load_rules` just hits the "missing file, no rules" branch. Ship a `notifications.yaml.example` or document the path.
- [ ] **`frontend/src/pages/settings/Tokens.vue` is an empty placeholder.** Fine for v1 MVP but `requirements.md §7.2` implies this is implemented; update the docs row or the component before committing.
- [ ] **`README.md` + `README_ZH.md` claim "Install the reporter client and drop a yaml config" but the client has `auth_token=...` kw-only API (no yaml autoloader).** The README shows a `monitor.yaml` pattern that isn't yet implemented in `argus.reporter`. Either soften the README ("config loaded by your experiment project, not by the client") or add a `ExperimentReporter.from_yaml()` helper.

## Nice-to-have (future work)

- Consider adding `vitest` with at least one store test on the frontend. Current frontend has 0 tests.
- `backend/backend/auth/jwt.py:44-48` blacklist is process-local; add a comment that multi-worker uvicorn breaks logout invalidation.
- `backend/backend/utils/ratelimit.py` similar — single-process only.
- `event_v1.json` has `additionalProperties: false` at the envelope level; the test `test_extra_envelope_field_rejected` relies on this. But `data: {...}` is `additionalProperties: true` by inheritance (no schema for `data`). Worth documenting that the tolerance for unknown fields is deliberate.
- Token prefix sniffing in `deps.py:106`: future tokens that don't use `em_live_/em_view_` (e.g. `em_prod_` for production) will silently fall through to JWT branch and fail confusingly. Centralise the prefix list in `auth/tokens.py`.
- `backend/backend/api/events.py:453-463` persists the raw event unconditionally before running per-type validation (line 443-452). That's actually fine — but the line 443 validation *raises* HTTPException, so the event row is rolled back by the endpoint-level commit boundary. Double-check that the SQLite write is never flushed before validation (currently `session.add` + `flush` happen at line 464-465, after validation — OK).

---

## Findings (detail per dimension)

### A. Correctness & Security — **3/5**

**Strong**:
- Argon2id hashing with library defaults (`backend/auth/password.py:19`) — correct choice.
- JWT HS256 with `require=[exp, iat, user_id, iss]` + issuer validation (`backend/auth/jwt.py:168-170`).
- Token storage uses SHA-256 hash + 8-char display hint (`backend/auth/tokens.py:85`) — plaintext never stored.
- Secrets module used for all random token generation (`backend/api/auth.py:96, 204`, `backend/auth/tokens.py:83`).
- No hardcoded secrets in the repo (dev JWT sentinel is clearly labeled and warned about in both `config.py:30` and startup `config.py:117-121`).
- FastAPI + SQLAlchemy ORM parameterised queries throughout — no string SQL concatenation found.
- Frontend: no `v-html`, no `innerHTML` — Vue's auto-escape is relied on.
- CSRF: Bearer-header-only auth → naturally immune (no cookies).
- Lockout after 5 failed logins with per-account counter (`providers/local.py:110-126`) — matches `requirements §4.3`.
- Login always returns identical 401 regardless of username existence (`api/auth.py:261-266`) — no user enumeration.

**Problematic**:
- `backend/db.py:34` env-var mismatch (see Must-fix).
- `api/auth.py:175-217` first-user-admin race (see Should-fix).
- `deps.py:122-126` un-tracked `asyncio.create_task` on every request (see Should-fix).

### B. Test Coverage & Quality — **2/5**

- **Backend: 33/52 pass, 19 fail.** The failures are not subtle — every event-ingest test panics on 401 because Phase 1 added `require_reporter_token` without updating the tests. Whoever signed off on "52 passed" did not run the suite on the current tree.
- **Client: 22/22 pass.** 34s runtime; well-structured; covers init / auth / timeout / spill / replay / idempotent retry / queue full / disable env / context manager. Quality is high.
- **Frontend: 0 tests.** Per scope note ("v1 single-user MVP UI; typecheck + build pass") that's accepted; both `pnpm run typecheck` and `pnpm run build` confirmed green on this review.

**Quality of backend tests that DO pass**:
- `test_auth_register.py`, `test_auth_login.py`, `test_auth_email_verify.py`, `test_auth_password_reset.py`, `test_auth_jwt.py` are thorough — cover happy path + 5 negative cases each.
- Good use of fixtures (`client`, `email_service`); no over-mocking.
- `test_auth_jwt.py::test_refresh_preserves_user_and_claims` sleeps 1.05s for deterministic iat difference — OK for MVP but adds 2s to suite runtime (could be replaced with monkeypatched `datetime.now`).

### C. Maintainability & Consistency — **4/5**

- Directory structure matches `design.md §3` very closely (minor: `schemas/` became a package, imports re-exported).
- Naming consistent: snake_case for Python, PascalCase for Vue components + TS types, camelCase for TS functions.
- Function length reasonable; longest functions are `_ingest_one` (54 lines) and `register` (77 lines) — within norms.
- Docstrings are dense and explain "why" (e.g., `db.py:50` StaticPool for in-memory, `auth/tokens.py:42-45` display_hint rationale).
- Dependency direction clean: backend never imports frontend; client is a wholly separate package with its own `pyproject.toml`.
- TODO markers are all scoped (e.g., `TODO(BACKEND-C)`) so the next agent can route.
- Minor inconsistency: `get_db` (in deps) vs `get_session` (direct from db). Pick one.

### D. API Contract Consistency — **4/5**

- `schemas/event_v1.json` title says v1.1 (line 3); the `schema_version` property is `const: "1.1"` (line 18) — **strict**. Yet backend accepts both 1.0 and 1.1 (`api/events.py:54`), and the sample tests emit `"1.0"`. There's no outright contradiction because the JSON Schema is only used as a client-side hint, but the authoritative contract doc diverges from what the server enforces. Pick a story: either schema file says "1.1" and backend rejects 1.0, or schema file has `enum: ["1.0", "1.1"]` and docs explain the transition.
- Client always sends v1.1 (`client/argus/schema.py:23`) + `event_id` (line 88-89). Backend's `_check_event_id_presence` (line 407) correctly requires event_id on v1.1 events.
- Pydantic `EventIn` (`backend/schemas/events.py:164-199`) matches JSON Schema envelope except for two subtleties:
  - `event_id` is `min_length=8, max_length=64` (line 192-193) but the JSON Schema says `format: uuid` (line 12). Consequence: the backend will accept short non-UUID strings (as long as they're 8-64 chars). Client tests don't exercise this.
  - `source.additionalProperties=false` is enforced (line 26); matches schema.
- `RegisterOut.user_id: int` matches the doc. `TokenCreateOut.token` returned only once (line 82-88) — matches requirements §4.2.

### E. Documentation — **4/5**

- `docs/requirements.md` is complete and opinionated; covers §1-18 including IA/dashboard.
- `docs/design.md` is synced with requirements; sequence diagrams match endpoint specs.
- `docs/architecture.md` (not read in this review) is referenced as "original data-model notes".
- `README.md` + `README_ZH.md` are clean, well-written, accurate. One minor drift: README tells users to `pip install argus-reporter` but the package isn't on PyPI yet.
- Deploy `Dockerfile` and `deploy/systemd/` referenced but `deploy/` contents not inspected here.

### F. Dependencies & Packaging — **4/5**

- `backend/pyproject.toml`: sane version floors (`fastapi>=0.115`, `sqlalchemy[asyncio]>=2.0`, `pydantic>=2.5`, `argon2-cffi>=23.1`, `pyjwt>=2.8`). No upper bounds — risky in principle but consistent with modern Python conventions. Dev deps properly split.
- `client/pyproject.toml`: minimal runtime dep (`requests>=2.31`); dev deps include `pytest-httpserver` + `pytest-timeout` + `jsonschema`. Clean.
- `frontend/package.json`: normal Vue 3 / Vite 5 / AntD set; pnpm-lock committed; no suspicious deps.
- `.gitignore` covers `.env*`, `*.db`, `backend/data/`, `node_modules/`, build artifacts, `__pycache__`, `.pytest_cache` — solid.

### G. Security & Prod Readiness — **3/5**

- Auth middleware is correctly explicit — there's no "open route list", every protected endpoint declares `Depends(get_current_user)` or stricter.
- CORS whitelist derived from `base_url` + always-allow localhost:5173 (`app.py:92-106`). Reasonable.
- Rate limit in place for ingest (600/min), but **no rate limit on login**. Combined with argon2 (~200ms/hash) this caps brute-force at ~5 attempts/sec which is bad news at 10k-rps scale — acceptable for MVP but worth noting.
- Auth-exempt paths are implicit (interceptor bypass list in `frontend/src/api/client.ts:57-64`); backend has **no** auth-exempt list because every public endpoint is unauthenticated by convention. No issue.
- `errors.md` style: HTTPException detail strings do not leak stack traces (checked `api/events.py:559-567` leaks `repr(exc)` which is the only place — flagged in Should-fix).
- Audit log table declared in `requirements §5.1` but not created yet (Phase 2 scope), and no audit-log writes wired into BACKEND-A endpoints. Acceptable per scope note.
- HTTPS: production `MONITOR_BASE_URL` expected to be HTTPS; Bearer-in-cleartext warned in README.

---

## Running the suite — actual results

```
# backend
$ pytest -q   (backend/)
19 failed, 33 passed in 9.55s

# client
$ pytest -q   (client/)
22 passed in 34.74s

# frontend
$ pnpm run typecheck    → clean
$ pnpm run build        → built in 37s, dist/ 494kB main chunk (with ECharts)
```

**Interpretation**: backend is NOT "52 tests passing". Phase 1 introduced `require_reporter_token` on event POSTs without retrofitting the test suite. This is the blocker for commit.

---

## Guidance for authoring agents

**backend-auth-eng**:
- Unblock the test suite: add a `reporter_token(client)` fixture in `conftest.py` that registers a user + mints an `em_live_` token + returns headers. Retrofit `test_events_post.py`, `test_idempotency.py`, `test_schema_validation.py`, `test_batches.py` to use it (the last one POSTs events for setup, same root cause).
- Update `test_wrong_schema_version_is_400` → `test_wrong_schema_version_is_415` to match the actual return code.
- Fix `db.py` to consume `get_settings().db_url` (eliminate the env-var duplication). Remove the back-populate hack in `config.py:104-105`.
- Add a thin `@pytest.fixture reporter_headers` so future ingest tests don't re-invent this.

**client-schema-eng**:
- Current v1.1 schema + reporter look good. Consider exposing a `ExperimentReporter.from_yaml()` classmethod so the README's "drop a monitor.yaml" recipe actually works without extra glue in user code.
- The `examples/callback_style.py` referenced in README was not read — make sure it exists and runs.

**frontend-eng (v1)**:
- Add `vitest` + one smoke test for `useAuthStore.login` to have a non-zero baseline. Not blocking.
- Settings/Tokens.vue is a placeholder — either hide the sidebar link in v1 or add a "coming soon" notice (the a-empty is acceptable).
- Small: `frontend/src/api/client.ts:43` declares `LoginLockedError` in the same module the axios interceptor uses. That's a small cycle risk; fine for now.

**docs**:
- Note in `requirements.md §10.2` that the **only** DB env var is `MONITOR_DB_URL` (not `EXPERIMENT_MONITOR_DB_URL`) once the fix lands.
- `schemas/event_v1.json` title currently says v1.1 but the server accepts 1.0 too. Either bump the file's filename to reflect the transition or add an `enum` to the `schema_version` property.

---

## Commit readiness checklist

- [x] `.gitignore` complete (covers secrets, DBs, build artifacts)
- [x] No hardcoded secrets (only labeled dev JWT sentinel, warned at startup)
- [x] README accurate (minor drift on `pip install argus-reporter` which isn't on PyPI yet)
- [ ] Tests all green — **blocked**: backend 19/52 failing
- [ ] Doc + code consistency on env var names — **blocked**: `MONITOR_DB_URL` vs `EXPERIMENT_MONITOR_DB_URL`
- [x] Docstrings / comments in place
- [x] Typecheck clean (frontend)
- [x] Build clean (frontend)
- [x] No secrets committed

---

## Recommendation

**Do not commit yet.** Two must-fix items — the runtime DB-URL divergence and the 19-failing-test suite — are easy to fix (combined maybe 1-2 hours) and must be fixed together. Once done, all of the should-fix items can safely defer to a subsequent PR.

Once the must-fix items land, the code is in genuinely solid shape for an MVP Phase 1 — the auth surface is well-designed, argon2id/JWT/SHA256 are used correctly, the schema v1.1 contract is consistent, and the client reporter handles spill/replay/idempotency as promised.
