# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Argus is an open-source, multi-user dashboard for monitoring long-running ML experiments across hosts. A FastAPI backend persists events from training scripts (pushed via the `argus-reporter` Python SDK) into SQLite or PostgreSQL, and a Vue 3 SPA renders batches, jobs, loss curves, and GPU/CPU resource sparklines live over Server-Sent Events. Single-image Docker deploy, Apache-2.0, bilingual (English / Simplified Chinese) UI and docs.

Layout:

- `backend/` — FastAPI service, async SQLAlchemy 2.0 models, Alembic migrations
- `frontend/` — Vue 3 + TypeScript + Vite SPA, served as static files in production
- `client/` — `argus-reporter` Python SDK (PyPI), plus framework integration adapters
- `schemas/` — versioned event contract (`event_v1.json`)
- `deploy/` — Dockerfile, compose file, nginx snippet, deploy script
- `docs/` — MkDocs Material site (operations, user guide, design, SDK reference)

## v0.2 features (what changed since v0.1.4)

The v0.2 cycle adds executor-style rerun, security hardening on tokens, multi-user notification routing, runtime-editable settings, and a substantial UI refresh. Key additions:

- **Executor / argus-agent daemon (#103)** — hosts run a long-lived `argus-agent` console-script (bundled in the `argus-reporter` wheel) that registers with Argus (`POST /api/agents/register` mints an `ag_live_…` token cached at `~/.argus-agent/token.json` mode 0600), polls `/api/agents/{id}/jobs` every 10 s, and heartbeats every 30 s. The Rerun button on a finished batch now actually re-launches the recorded `env_snapshot.command` on its originating host via `subprocess.Popen(shell=True)`. See `docs/ops/argus-agent.md` for the threat model.
- **JWT dual-key rotation (migration 031)** — `auth.jwt_active_kid` + `auth.jwt_signing_keys` live in `system_config`; tokens carry a `kid` claim and verification accepts any non-expired key. Rotating a key has a 60 s cooldown to avoid stampedes; old tokens stay valid until their own `exp`.
- **Multi-recipient project notifications (#116)** — projects route email events to a list of owners/admins, not just the creator.
- **Per-user notification preferences (#108)** — Settings panel toggles `email_on_batch_done`, `email_on_job_failed`, `daily_digest`, plus GitHub linking, and email-change with verify-link + 1/min resend cooldown.
- **DB-driven runtime config with Fernet (#107)** — GitHub OAuth, SMTP, retention caps, demo toggle, and feature flags edit live from Settings → Admin without redeploy. Secrets encrypt at rest with a Fernet key derived from `ARGUS_CONFIG_KEY` (preferred) or `ARGUS_JWT_SECRET` (fallback). Read precedence: DB row > `ARGUS_*` env > default.
- **JobDetail refactor (#104)** — telemetry strip (status / elapsed / GPU util / GPU mem peak / latest loss) on top, embedded log tail in the middle, action bar (Stop / Rerun / Share / Copy command) at the bottom. Tab layout is gone.
- **JobMatrix redesign (#126)** — white default cell background; best-in-column highlighted green bold, worst-in-column italic red. Multi-metric column selector + CSV export retained.
- **Global `/jobs` page (#118)** — flat-list view with Status / Project / Host / Batch / Tags / `since` filters, deep-linkable from dashboard tiles. RBAC: non-admins only see jobs from their projects.
- **Hyperopt UI (Optuna trials)** — Studies tab visualises Optuna multirun sweeps when the SDK emits `optuna.{study_name, trial_number}` labels on `job_start`. Trial scatter, parallel-coordinates, parameter importance.
- **Light theme audit + 5-color status palette (#125)** — running = green (was blue), done = grey-green (was green), unified across dashboard / batches / jobs / watchdog. Visual breaking change vs. v0.1.x screenshots.
- **SSE multiplex** — single Server-Sent Events connection per page replaces the previous N+2 pattern (one per batch + dashboard + watchdog), reducing connection pressure on the proxy and the browser tab budget.
- **Lightning + Keras SDK adapters** — `client/argus/integrations/lightning.py` and `client/argus/integrations/keras.py` ship `ArgusCallback` drop-ins. Optional extras: `pip install argus-reporter[lightning]` / `[keras]` / `[all-integrations]`. Imports are lazy so missing optional deps do not break the rest of the SDK.

### Alembic chain (v0.2)

The migrations form a single linear chain after the 026 merge:

```
… 023 → 024 (project notif recipients)
       └→ 025 (system_config)         ─┐
                                        ├→ 026_merge_024_025 → 027_executor_agent
                                                                 → 028_user_notification_prefs
                                                                 → 030_token_user_binding
                                                                 → 031_jwt_dual_key
```

`alembic upgrade head` works on a fresh DB; the parallel 024/025 branches were linearised in 026 so upgrades from any v0.1.x snapshot are safe. Migration 031 chains after 030; the JWT dual-key fix re-anchored 031 onto 030 to keep the chain linear after the parallel feature branches landed.

The post-v0.2.1 head is `…028 → 030 → 031` (single linear head `031_jwt_dual_key`):

- `030_token_user_binding` — Postgres-compat hotfix using `is_admin IS TRUE` (boolean comparison, not implicit truthiness).
- `031_jwt_dual_key` — `system_config` seed wraps the JSON payload in an explicit `CAST(... AS JSON)` so asyncpg can bind the parameter without a `DataError: invalid input for query argument`.
- Operator note for production upgrades: `alembic_version.version_num` was historically declared `VARCHAR(32)`. Revision IDs longer than 32 chars require a one-time `ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)` on existing PostgreSQL deployments before `alembic upgrade head` will run cleanly.

## v0.2.1 — surface additions

v0.2.1 is a thin layer on top of v0.2: one new SDK adapter, batch-id continuity for resumable benchmark runs, and a docs reorg. No schema changes, no breaking wire changes.

- **Hydra SDK adapter (#139)** — `client/argus/integrations/hydra.py` exposes `class ArgusCallback(hydra.experimental.callback.Callback)` following the same lazy-import pattern as the Lightning and Keras adapters; importing the SDK without Hydra installed stays free. Single-run jobs are wired through `on_run_start` / `on_run_end`; multirun sweeps go through `on_multirun_start` plus per-trial `on_job_start` / `on_job_end` so each Optuna or grid trial appears as its own job under a shared batch. Install via `pip install argus-reporter[hydra]`. Reference: [`docs/sdk/hydra-callback.md`](docs/sdk/hydra-callback.md).
- **Batch identity continuity (#141)** — benchmark scripts that crash and resume now write to the same Argus batch instead of creating a new one each run.
  - `argus.identity.derive_batch_id(project, experiment_name, git_sha=None, *, prefix="bench")` returns `<prefix>-<16hex>` from a SHA-256 of `(project|experiment|git_sha or "no-git")`. Same inputs always derive the same id, so a resumed run finds the original batch deterministically.
  - `Reporter(batch_id=…)` and `Reporter(resume_from=…)` kwargs override the default UUID minted per process. Pass the original id from the crashed run to continue it.
  - Backend `events.py:_handle_batch_start` is now idempotent: a second `batch_start` for an existing `batch_id` preserves the original `start_time` and refuses to flip a `done` batch back to `running`.
  - Wire-compat: the same derivation algorithm is reproduced inline in Sibyl (`sibyl/identity.py`) so reporter and orchestrator agree on the id without a runtime SDK dependency on either side.
  - Reference: [`docs/sdk/resume.md`](docs/sdk/resume.md).

  ```python
  from argus import Reporter
  from argus.identity import derive_batch_id

  batch_id = derive_batch_id("deepts-flow", "etth1_dlinear_seed42", git_sha="abc1234")
  reporter = Reporter(
      base_url="https://argus.example.com",
      token="em_live_xxxxx",
      batch_id=batch_id,        # same id across restarts
      # resume_from=batch_id,   # equivalent alias
  )
  ```

- **Docs reorg (scheme A)** — bilingual mirror layout: every English page under `docs/X.md` has a sibling `docs/zh/X.md`. The previous `docs/X.zh-CN.md` suffix convention is dropped. Top-level `REVIEW_TEAM_*.md` files moved into `docs/reviews/`. Locale switching is handled by the mkdocs `static-i18n` plugin; CLAUDE.md is intentionally left out of the mkdocs nav and remains English-only project context.
- **`CLAUDE.md` gitignored** — `.gitignore` now lists `CLAUDE.md`. The currently tracked file stays in the repo so contributors can read project-internal context, but new edits will not be tracked unless explicitly force-added (`git add -f CLAUDE.md`). Treat it as project-scope reference, not a release artifact.

## Common Commands

### Backend

```bash
# Local dev (SQLite, port 8000)
cd backend
pip install -e ".[dev]"
alembic upgrade head
uvicorn backend.app:app --reload

# Tests
pytest
pytest backend/tests/test_jwt_rotation.py -v
```

### Frontend

```bash
cd frontend
pnpm install
pnpm dev          # vite dev server, proxies to localhost:8000
pnpm build        # production bundle, picked up by the backend container
pnpm test         # vitest
```

### SDK

```bash
cd client
pip install -e ".[dev]"
pytest

# Build wheel
python -m build
```

### Docker (full stack)

```bash
cd deploy
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"  # paste into ARGUS_JWT_SECRET
docker compose up -d --build
# open http://localhost:8000
```

### argus-agent on a training host

```bash
pip install argus-reporter
export ARGUS_BASE_URL=https://argus.example.com
export ARGUS_TOKEN=em_live_...    # SDK token from Settings → Tokens
argus-agent --register            # mints ag_live_… and caches it
argus-agent                       # foreground; or wrap with systemd
```

### Docs site

```bash
pip install -r requirements-docs.txt
mkdocs serve
mkdocs build --strict
```

## Architecture (v0.2)

```
training host                    argus server                            browser
-------------                    ------------                            -------
Reporter / SDK ────POST /api/events────► FastAPI (4-worker uvicorn)      Vue 3 SPA
   │                                       │                              ▲
argus-agent ◄──poll /api/agents/{id}/jobs──┤    async SQLAlchemy 2.0      │
   │ subprocess.Popen rerun                │      │                       │
   │ SIGTERM stop                          │      ▼                       │
   └──ack + heartbeat──────────────────────┤    SQLite (default)          │
                                           │      or PostgreSQL           │
                                           │                              │
                                           │  multiplexed SSE stream ─────┘
                                           │  (one connection per page)
                                           │
                                           │  retention / backup / email worker
                                           │  (fcntl singleton lock)
```

Wire-compat: SDK `argus-reporter` >= 0.4 is the matching client for v0.2; older 0.3 SDKs keep working but cannot register agents.

## Adding Components

### A new SSE event channel
1. Add an enum entry in `backend/backend/services/sse.py`.
2. Subscribe it on the multiplex hub (one connection demuxes by channel name).
3. Bump `event_v1.json` minor version only if the wire shape changes.

### A new framework integration
Drop `client/argus/integrations/<framework>.py` exporting `ArgusCallback`. Use the lazy-import pattern from `lightning.py` / `keras.py` / `hydra.py` so missing optional deps do not break the SDK install. Add an extra in `pyproject.toml`.

### A new Alembic migration
1. Always chain after the current head — never branch unless you are paying for a merge revision in the same PR.
2. Name file `NNN_short_description.py` matching the leading number convention.
3. Run `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` against both SQLite and PostgreSQL before merging.

## Documentation

- [`docs/ops/argus-agent.md`](docs/ops/argus-agent.md) — agent install, register, threat model
- [`docs/ops/admin-settings.md`](docs/ops/admin-settings.md) — DB-driven runtime config
- [`docs/user-guide/notifications.md`](docs/user-guide/notifications.md) — per-user prefs + project recipients
- [`docs/sdk/hydra-callback.md`](docs/sdk/hydra-callback.md) — Hydra SDK adapter, single-run + multirun
- [`docs/sdk/resume.md`](docs/sdk/resume.md) — batch identity continuity and crash-resume protocol
- [`docs/reviews/`](docs/reviews/) — review-team notes (moved from repo root in v0.2.1)
- [`CHANGELOG.md`](CHANGELOG.md) — full v0.2 release notes
