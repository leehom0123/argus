> 🌐 **English** · [中文](./README.zh-CN.md)

# Argus backend

FastAPI service that ingests events from ML experiment runners and exposes a
small JSON API for the Vue frontend.

## Install

```bash
cd /path/to/argus/backend
pip install -e ".[dev]"
```

Requires Python 3.10+.

## Run dev server

```bash
# 1. One-time (and after every schema change): run Alembic manually.
alembic upgrade head

# 2. Start the API.
uvicorn backend.app:app --reload --port 8000
```

The app serves:

- `/api/*` — ingest + read endpoints (see `docs/architecture.md`)
- `/health` — liveness probe
- `/` — built frontend (if `../frontend/dist/` exists); otherwise API-only

SQLite file defaults to `backend/data/argus.db`. Override with
`ARGUS_DB_URL=sqlite+aiosqlite:///...`.

### Migrations are operator-driven

The process does **not** auto-run Alembic on startup. The in-process
`command.upgrade` path we used to ship was a no-op inside an already
running asyncio loop and we removed it rather than smuggle a thread-pool
workaround in. Run `alembic upgrade head` yourself before bringing up
uvicorn; fresh installs also get missing tables via
`Base.metadata.create_all` inside `init_db()` as a convenience for
tests and one-off dev sandboxes.

### Production: hide `?token=` from access logs

Browser `EventSource` requests cannot attach an `Authorization` header,
so the SSE stream accepts a `?token=<JWT>` fallback. The app installs a
`logging.Filter` on the `uvicorn.access` logger that rewrites every
`token=...` query parameter to `token=REDACTED` before the line is
emitted. This is a best-effort safety net — the recommended production
posture is still to terminate TLS on nginx (or similar) and strip the
query parameter there, e.g.

```nginx
location /api/events/stream {
    # drop ?token=... from $request so it never touches our access log
    set $scrub_args $args;
    if ($scrub_args ~ (.*)(^|&)token=[^&]*(.*)) {
        set $scrub_args $1$2$3;
    }
    access_log /var/log/nginx/em.log combined;
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
}
```

## Run tests

```bash
pytest -q
```

Tests use an in-memory SQLite DB per test so they run cleanly and in parallel.

## Notifications

Rules live in `backend/config/notifications.yaml` (gitignored; copy
`backend/notifications/config.yaml.example` to start). The feishu webhook URL
can also be supplied via `ARGUS_FEISHU_WEBHOOK`.

Only a restricted DSL is supported in `when:` (no `eval`):

```
event_type == "job_failed"
event_type == "batch_done" and data.n_failed > 0
data.gpu_util_pct < 10
```

## Data retention

The backend runs an in-process sweeper that deletes old rows on a configurable
schedule. `batch` and `job` rows are **never** deleted — they are result
archives. Everything else has a per-class age cap:

| Env var | Default | Scope |
|---|---|---|
| `ARGUS_RETENTION_SNAPSHOT_DAYS` | 7 | `resource_snapshot` rows (non-demo hosts) |
| `ARGUS_RETENTION_LOG_LINE_DAYS` | 14 | `event` rows where `event_type = 'log_line'` |
| `ARGUS_RETENTION_JOB_EPOCH_DAYS` | 30 | `event` rows where `event_type = 'job_epoch'` |
| `ARGUS_RETENTION_EVENT_OTHER_DAYS` | 90 | all other `event` rows |
| `ARGUS_RETENTION_DEMO_DATA_DAYS` | 1 | demo-host snapshots (shorter cap) |
| `ARGUS_RETENTION_SWEEP_MINUTES` | 60 | how often the sweeper runs |

The sweeper runs in-process as an `asyncio` background task started during
lifespan. Set `ARGUS_RETENTION_SWEEP_MINUTES=0` to disable the background
loop entirely (useful when running an external cron job instead).

Admins can trigger a manual sweep via `POST /api/admin/retention/sweep` and
check the current settings + last/next run via `GET /api/admin/retention/status`.

> **TODO:** a `ARGUS_RETENTION_BATCH_DAYS` knob for batch/job archiving can
> be added later; the DB design supports it (soft-delete via `is_deleted`).

## Event contract

Authoritative schema: `../schemas/event_v1.json`. The backend only accepts
events with `schema_version: "1.1"`; any other value returns **415
Unsupported Media Type** with `{"detail": "Unsupported schema_version",
"supported": ["1.1"]}`. v1.1 requires a client-generated UUID `event_id`
for idempotent retries (see `docs/requirements.md` §6.5 and
`../client/argus/schema.py`).

### GitHub OAuth

Set these env vars to enable "Sign in with GitHub":

```
ARGUS_GITHUB_OAUTH_ENABLED=true
ARGUS_GITHUB_CLIENT_ID=...
ARGUS_GITHUB_CLIENT_SECRET=...
```

All three must be present — missing any one keeps `/api/auth/oauth/github/*`
returning 404 and the login page hides the button (see `/api/auth/oauth/config`).

Create an OAuth App at https://github.com/settings/developers with:

```
Authorization callback URL:
  {ARGUS_BASE_URL}/api/auth/oauth/github/callback
```

A GitHub sign-in whose primary verified email matches an existing
local user **links** the identity (OAuth becomes a second sign-in
method; the password still works). Otherwise a fresh user is
provisioned with `auth_provider='github'` and no local password.
