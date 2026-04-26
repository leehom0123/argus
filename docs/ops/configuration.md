# Configuration

Argus has **two** configuration tiers:

1. **Bootstrap env vars** (this page) — read at process start. All names use
   the `ARGUS_` prefix.
2. **Runtime config** ([Admin settings](admin-settings.md)) — DB-backed,
   editable from the UI without redeploy. Covers GitHub OAuth, SMTP,
   retention, demo project, and feature flags.

For overlap (e.g. SMTP set both via env and via DB), the **DB row wins**;
env values are seed defaults.

The truth source for what each variable does is `backend/backend/config.py`
(Pydantic `Settings` class) plus a few `os.environ.get` lookups in
`backend/backend/app.py` and `services/secrets.py` for the variables that
are read outside the `Settings` model.

## Required-ish

Argus boots with safe development defaults, but in production you must set:

| Variable | Notes |
|---|---|
| `ARGUS_JWT_SECRET` | Session-signing key. ≥32 bytes enforced when `ARGUS_ENV=prod`. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `ARGUS_ENV` | `prod` enables strict checks (long secret, hard-fail on misconfig). Default `dev`. |
| `ARGUS_BASE_URL` | Public URL used in email links and CORS allow-list. Default `http://localhost:5173`. |
| `ARGUS_CONFIG_KEY` | Fernet key for encrypting runtime-config secrets in the DB. Falls back to `ARGUS_JWT_SECRET` if unset, but you should set it explicitly so JWT rotation never invalidates encrypted config. |

## Database

| Variable | Default | Notes |
|---|---|---|
| `ARGUS_DB_URL` | `sqlite+aiosqlite:///<backend>/data/argus.db` | Async SQLAlchemy URL; for Postgres: `postgresql+asyncpg://user:pw@host/db` |
| `ARGUS_DB_POOL_SIZE` | per-dialect default | Compose sets 10 |
| `ARGUS_DB_POOL_MAX_OVERFLOW` | per-dialect default | Compose sets 15 |
| `ARGUS_DB_POOL_TIMEOUT` | per-dialect default | |
| `ARGUS_DB_POOL_RECYCLE` | per-dialect default | |

## SMTP (also editable in Admin UI)

| Variable | Default |
|---|---|
| `ARGUS_SMTP_HOST` | empty → emails print to stdout |
| `ARGUS_SMTP_PORT` | 587 |
| `ARGUS_SMTP_USER` / `ARGUS_SMTP_PASS` | empty |
| `ARGUS_SMTP_FROM` | `noreply@argus.local` |
| `ARGUS_SMTP_USE_TLS` | true |

## Notifications

| Variable | Notes |
|---|---|
| `ARGUS_FEISHU_WEBHOOK` | Optional Feishu bot webhook URL; receives notification cards. Read by `notifications/watchdog.py` and registered into `app.state.notification_channels`. |

## GitHub OAuth (also editable in Admin UI)

| Variable | Default |
|---|---|
| `ARGUS_GITHUB_OAUTH_ENABLED` | false |
| `ARGUS_GITHUB_CLIENT_ID` | empty |
| `ARGUS_GITHUB_CLIENT_SECRET` | empty |

These env vars are seed defaults; the runtime source of truth is the DB row
read via `backend.api.oauth._get_github_oauth_state(db)`. Edit in
**Settings → Admin → OAuth (GitHub)**.

## Retention (also editable in Admin UI)

| Variable | Default (days) |
|---|---|
| `ARGUS_RETENTION_SNAPSHOT_DAYS` | 7 |
| `ARGUS_RETENTION_LOG_LINE_DAYS` | 14 |
| `ARGUS_RETENTION_JOB_EPOCH_DAYS` | 30 |
| `ARGUS_RETENTION_EVENT_OTHER_DAYS` | 90 |
| `ARGUS_RETENTION_DEMO_DATA_DAYS` | 1 |
| `ARGUS_RETENTION_SWEEP_MINUTES` | 60 (`0` disables the in-process sweeper) |

Summary rows (one per batch and one per job) are never purged.

## Guardrails

| Variable | Default | Notes |
|---|---|---|
| `ARGUS_DIVERGENCE_RATIO` | 2.0 | Fire when val_loss grows by ≥ this over the window |
| `ARGUS_DIVERGENCE_WINDOW` | 3 | Consecutive epochs |
| `ARGUS_IDLE_JOB_THRESHOLD_MIN` | 10 | Idle when GPU util <5% for this many minutes |
| `ARGUS_STALL_TIMEOUT_MIN` | 15 | Mark batch *stalled* after this much silence |
| `ARGUS_STALL_CHECK_INTERVAL_S` | 120 | How often the stall detector runs |
| `ARGUS_BATCH_DIVERGENCE_CHECK_INTERVAL_S` | 60 | Same idea for divergence |
| `ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED` | true | Email on login from a new (IP, UA) pair |

## Backups (SQLite)

| Variable | Default |
|---|---|
| `ARGUS_BACKUP_INTERVAL_H` | 6 (`0` disables the loop) |
| `ARGUS_BACKUP_KEEP_LAST_N` | 7 |

## JWT

| Variable | Default |
|---|---|
| `ARGUS_JWT_SECRET` | dev sentinel (rejected in prod) |
| `ARGUS_JWT_ALGORITHM` | `HS256` |
| `ARGUS_JWT_ISSUER` | `argus` |
| `ARGUS_JWT_TTL_SECONDS` | 86400 (24 h) |

## Auth policy

| Variable | Default |
|---|---|
| `ARGUS_LOGIN_MAX_FAILURES` | 5 |
| `ARGUS_LOGIN_LOCK_MINUTES` | 10 |
| `ARGUS_EMAIL_VERIFY_TTL_HOURS` | 24 |
| `ARGUS_PASSWORD_RESET_TTL_MINUTES` | 15 |
| `ARGUS_EMAIL_CHANGE_TTL_HOURS` | 168 (7 d) |
| `ARGUS_PASSWORD_MIN_LENGTH` | 10 |

## Scaling / process

| Variable | Default | Notes |
|---|---|---|
| `ARGUS_WORKERS` | 4 | Read by `deploy/entrypoint.sh`; controls `--workers` on uvicorn. Set to 1 for the SQLite dev loop. |
| `ARGUS_LOCK_DIR` | `/tmp` | Where fcntl advisory locks live for the singleton tasks (retention sweep, backup, watchdog). Override in tests. |

## Logging

| Variable | Default |
|---|---|
| `ARGUS_LOG_LEVEL` | `INFO` |

## SDK side (read by `argus-reporter`)

The SDK reads its own env vars (not the backend's). Listed here for reference:

| Variable | Notes |
|---|---|
| `ARGUS_URL` | Backend URL. **Not the same as `ARGUS_BASE_URL`** — `ARGUS_BASE_URL` is the backend's view of its own public URL; `ARGUS_URL` is what the SDK posts to. |
| `ARGUS_TOKEN` | `em_live_…` SDK token |
| `ARGUS_DISABLE` | `1` makes the SDK a no-op |

## Note on `ARGUS_CORS_ORIGINS`

The shipped `deploy/docker-compose.yml` and `.env.example` mention this
variable. It is **not** read by the backend today — `_cors_origins()` in
`backend/backend/app.py` derives the allow-list from `ARGUS_BASE_URL` plus
hardcoded `http://localhost:5173` and `http://localhost:8000`. The compose
entry is a placeholder for a future change.

## See also

* [Admin settings](admin-settings.md) — runtime config UI.
* [Database](database.md) — pool tuning & dialect choice.
