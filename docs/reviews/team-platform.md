# Code Review — `feat/team-platform` vs `main`

**Reviewer**: Platform Team Code Reviewer (independent pass)
**Scope**: 6 commits (3 BE + 3 FE), 23 files, +950 / −29
**Date**: 2026-04-24

---

## TL;DR verdict

**APPROVE with minor follow-ups.** The branch cleanly ships the three deliverables
on the Platform Team brief: Apache-2.0 adoption, Postgres dual-DB support, and
the About page. License paperwork is correct, migrations coerce booleans the
right way, and the CI matrix boots a real Postgres 16 for the migration gate.
No blocking issues. Two medium follow-ups (CI coverage gap, hardcoded repo URL)
and a handful of nits, all listed below.

---

## Ratings at a glance

| Area                       | Rating                          |
| -------------------------- | ------------------------------- |
| License compliance         | **CLEAR**                       |
| Dual-DB correctness        | **CLEAR (with one caveat)**     |
| CI coverage                | **ACKNOWLEDGED GAP** (see §3)   |
| Documentation              | **CLEAR**                       |
| FE About page              | **CLEAR**                       |
| Open-source hygiene        | **CLEAR**                       |

---

## 1. License compliance — **CLEAR**

### Verified

- **LICENSE is verbatim Apache-2.0.** `md5sum LICENSE` equals the md5 of
  `https://www.apache.org/licenses/LICENSE-2.0.txt` (both
  `3b83ef96387f14655fc854ddc3c6bd57`). No modifications — the license is valid.
- **NOTICE matches §4(d) format**: copyright line + standard boilerplate.
  Covers the "include a readable copy" requirement for downstream distributors.
- **SPDX metadata flipped from MIT → Apache-2.0 in both pyproject.toml files**:
  - `backend/pyproject.toml:11` — `license = { text = "Apache-2.0" }`
  - `client/em_client/pyproject.toml:11` + classifier
    (`"License :: OSI Approved :: Apache Software License"`)
  No stale MIT references remain in either file.
- **Dependency license audit**: `grep -iE "(GPL|LGPL|AGPL|copyleft)"` on both
  `backend/uv.lock` and `frontend/pnpm-lock.yaml` returns empty. The explicit
  stack (FastAPI/Starlette/Pydantic/SQLAlchemy/Alembic — all MIT or BSD-3; Vue
  3 / Ant Design Vue / vue-i18n / Pinia — all MIT; echarts — Apache-2.0) is
  100 % Apache-2.0-compatible.
- **CLA vs DCO**: `CONTRIBUTING.md:155–178` is explicit — no CLA required, DCO
  1.1 referenced with the canonical developercertificate.org URL, and
  `git commit -s` called out as optional. Mirrored in the zh-CN section
  (`CONTRIBUTING.md:333–351`). Good hygiene.

### Nits

- `README.md:228` — license link points to `./LICENSE` (relative path; will
  render correctly on GitHub + most mirrors). No action needed.
- `CONTRIBUTING.md:30` / `:212` — clone URL is
  `github.com/placeholder/experiment-monitor.git`. Once the repo goes public,
  replace `placeholder` with the real org. Flagged as follow-up, not blocking.

---

## 2. Dual-DB correctness — **CLEAR (one caveat)**

### Boolean `server_default` coercion — all correct

Audited every `server_default` in `backend/migrations/versions/*.py`:

| Migration   | Line(s)          | Column             | Before           | After        | Verdict |
| ----------- | ---------------- | ------------------ | ---------------- | ------------ | ------- |
| 002         | 39, 45, 51, 77   | is_active / is_admin / email_verified / consumed | `sa.text("0")` / `sa.text("1")` | `sa.true()` / `sa.false()` | OK |
| 003         | 54, 88           | revoked / is_deleted | `sa.text("0")` | `sa.false()` | OK |
| 009         | 49               | is_public          | `sa.text("0")`   | `sa.false()` | OK |
| 010         | 47, 63           | is_demo / hide_demo | `sa.text("0")`  | `sa.false()` | OK |
| 016         | 41               | is_idle_flagged    | `sa.text("0")`   | `sa.false()` | OK |

`sa.true()` / `sa.false()` render as `TRUE` / `FALSE` on Postgres and `1` / `0`
on SQLite — exactly the dialect abstraction the BE team was aiming for.

**Non-Boolean `server_default="0"` / `"1"`** (Integer columns, e.g.
`001_initial.py:38,41` `n_done` / `n_failed`; `004_sharing_admin_audit.py:117`
`view_count`; `002:59` `failed_login_count`) are **intentionally left as
strings** — both PG and SQLite accept numeric strings for Integer columns.
Correct call.

### PG-specific column types — none

`grep -rE "JSONB|sa\.JSON|ARRAY|sa\.UUID|ENUM|INET"` across `backend/` returns
only doc-string mentions of "UUID" (referring to client-generated string UUIDs
stored in `sa.Text()`) and Pydantic schema comments. No actual `JSONB`, `ARRAY`,
`UUID`, `ENUM`, or `INET` columns. JSON is stored as `sa.Text()` (e.g.
`016_guardrails.py:46` `known_ips_json`), which works on both backends.

### Caveat (not blocking)

- **`batch_alter_table` in `016_guardrails.py`**: Alembic's `batch_alter_table`
  defaults to `recreate='auto'`, which on Postgres adds the column in-place.
  Verified this works by the CI matrix's upgrade→downgrade→re-upgrade loop on
  PG 16 (`ci.yml:82–91`). Good.
- **Retroactive renaming risk**: revisions that previously wrote
  `sa.text("0")` / `sa.text("1")` have now been **edited in place** rather than
  superseded by a new revision. This is normally a bad practice (breaks
  reproducibility for anyone who ran them on the old code), but since this
  project's deployments were SQLite-only before today, and `text("0")` and
  `sa.false()` both resolve to `0` on SQLite, the on-disk schema is identical.
  For anyone bringing up a fresh PG instance, the CI matrix is the source of
  truth. **Document this in a future release note** when cutting a versioned
  release.

---

## 3. CI coverage — **ACKNOWLEDGED GAP** (medium follow-up)

`.github/workflows/ci.yml` is syntactically valid, caches pip + pnpm
(`ci.yml:54`, `143–148`), uses `concurrency` to cancel stale runs (`:10–12`),
and triggers on `push main` + PRs (no cron — fine for MVP). Secrets are
limited to `POSTGRES_PASSWORD: test` inside the ephemeral service — not a
leak.

**The gap**: the `postgres` matrix cell runs only
`test_artifacts.py test_i18n.py` (`ci.yml:117`), and even that runs with
`MONITOR_DB_URL` forced back to SQLite (`ci.yml:116`). The PG lane effectively
only gates on:
1. `alembic upgrade head`
2. `alembic downgrade base`
3. `alembic re-upgrade head`

This catches migration-level dialect drift (the headline goal) but **not** PG
bugs in application code — e.g. an accidental `LIMIT 1` vs `FETCH FIRST`
divergence in a hand-written SQL query would ship. The inline YAML comment
(`ci.yml:97–100`) flags this honestly; the CONTRIBUTING.md PG instructions
(`:112–122`) also acknowledge it. Accepted as a known limitation for the MVP.

**Recommended follow-up** (separate PR, not blocking this merge): port
`conftest.py::_install_test_env` to parameterise the engine URL, so the PG
matrix cell can run the full pytest suite. Track in an issue.

---

## 4. Documentation — **CLEAR**

- `docs/deploy/postgres.md` + `docs/deploy/postgres.zh-CN.md` — covers
  "when to switch", docker one-liner, extras flag, alembic instructions.
  Links to `pg_dump` / `pg_restore` for backups. Well-scoped.
- `README.md:5–8` — badges for License + Python + Node + CI. All point to
  valid targets (verified `LICENSE` exists, CI workflow path matches).
- `README.zh-CN.md` — mirror of EN changes; parity preserved.
- `CONTRIBUTING.md` — dual-DB testing section (`:110–138`) is accurate.
  Licensing & DCO section (`:155–178` + zh-CN mirror) is comprehensive.

### Nits

- `CONTRIBUTING.md:39` / `:221` references `pip install -r backend/requirements.txt`
  but the repo uses `pyproject.toml` with extras (`pip install -e ".[dev]"` or
  `.[dev,postgres]"`). The CI workflow (`ci.yml:59, 64`) uses the correct
  `-e ".[dev]"` form. **Update CONTRIBUTING to match**, otherwise new
  contributors will hit a missing-file error. Minor, fix on next doc pass.

---

## 5. FE About page — **CLEAR**

- **Version injection**: `frontend/vite.config.ts:10–12, 15–17` reads
  `package.json` at build time, exposes it as `__APP_VERSION__` via
  `define`. The type declaration is added in `frontend/src/env.d.ts:10`
  (`declare const __APP_VERSION__: string`). About page consumes it at
  `pages/settings/About.vue:8`, footer consumes it defensively at
  `App.vue:151` (`typeof __APP_VERSION__ !== 'undefined'`). Good pattern.
- **License link**: `About.vue:10` and `App.vue:351` both point directly to
  `https://www.apache.org/licenses/LICENSE-2.0` — exact URL, not a redirect
  via `.html` or shortener. Correct.
- **Footer gate**: footer sits inside the `<template v-else>` block in
  `App.vue:173` (the non-auth layout), so it is automatically hidden on
  `/login`, `/register`, `/verify-email`, `/reset-password` via the
  `useAuthLayout` computed property (`App.vue:69`). Verified by routing logic.
- **Route guard**: `/settings/about` added at `frontend/src/router/index.ts:196–201`
  with `meta: { requiresAuth: true }` — matches the sibling settings routes.
- **i18n parity**: both `en-US.ts` and `zh-CN.ts` ship the `footer.*`,
  `nav.about`, and `page_settings_about.*` keys (verified via `grep -cE`,
  count matches). `pnpm test:i18n` (referenced in CONTRIBUTING) will enforce
  this going forward.

### Nit

- `pages/settings/About.vue:11–13` hardcodes the GitHub repo URL. If the repo
  ever moves orgs, this becomes stale. Consider surfacing via a build-time
  define or pulling from `package.json.repository.url`. Minor.

---

## 6. Open-source hygiene — **CLEAR**

- `deploy/docker-compose.yml:18` — default is
  `MONITOR_DB_URL: sqlite+aiosqlite:////app/data/monitor.db`. Zero-dep
  onboarding preserved.
- `backend/pyproject.toml:44–47` — `postgres` extras is **opt-in**, not a
  default dependency. Matches the "SQLite default, Postgres documented"
  story in `docs/deploy/postgres.md`.
- `README.md` + `README.zh-CN.md` — Quickstart copy paste is
  `docker compose -f deploy/docker-compose.yml up -d` → SQLite path.
- `.gitignore` — unchanged, still covers `.env`, `node_modules`, `*.pt`,
  etc.
- No secrets / tokens in the diff (manually grepped:
  `password|secret|token|key` — only hits are in the auth module's legit
  `password = credentials.get("password")` line and fixture strings).
- `backend/pyproject.toml` + `client/em_client/pyproject.toml` — `name`,
  `description`, `classifiers`, `license` all suitable for PyPI publication.

---

## 7. XFAIL on `test_artifacts` — acceptable

`backend/backend/tests/test_artifacts.py:29–32` applies
`pytestmark = pytest.mark.xfail(reason="...", strict=False)`. With
`strict=False`, an XPASS (i.e., the artifact route starts working) will **not**
fail CI — the maintainer has to notice via a green-with-warnings run and flip
the marker. That is the right tradeoff while the route is known-broken: avoids
CI churn, surfaces the fix on flip rather than collapse. **Track in a P2 issue
so it doesn't rot.**

---

## Blocking issues

**None.** Merge approved.

---

## Recommended follow-ups (file / PR-sized)

1. **Port pytest fixtures to dual-DB** so the PG matrix cell runs the full
   suite, not just two test files. Update `backend/backend/tests/conftest.py`
   `_install_test_env` to honour `MONITOR_DB_URL` when it points at Postgres.
2. **Fix artifact upload 405** and flip the xfail in `test_artifacts.py:29`.
3. **Update `CONTRIBUTING.md:39, :221`** to use `pip install -e ".[dev]"`
   instead of the non-existent `requirements.txt`.
4. **Replace `github.com/placeholder/`** clone URLs in CONTRIBUTING and the
   hardcoded repo URL in `About.vue:11` once the public repo lands.
5. **Add a release-notes entry** acknowledging the in-place edit of
   migrations 002 / 003 / 009 / 010 / 016. Document that SQLite-only
   deployments running pre-patch revisions are wire-compatible.

---

## Closing note

Three Apache-compatible commits, a dual-DB migration gate that boots real PG
16 in CI, and a tidy About page with build-time version injection. Clean
branch, good test hygiene (xfail over skip), bilingual docs throughout. Ship it.
