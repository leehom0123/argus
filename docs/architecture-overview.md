# Architecture

A high-level walk through Argus's shape. For module-level design notes see
the legacy `docs/design/` directory in the repo (excluded from this site's
nav).

## System diagram

```
training host                     argus server                            browser
─────────────                     ─────────────                           ───────
Reporter (argus-reporter) ─POST /api/events─▶ FastAPI (uvicorn)            Vue 3 SPA
   │                                              │                         ▲
argus-agent ◀poll /api/agents/{id}/jobs──────────┤    async SQLAlchemy 2.0  │
(shipped by Sibyl)                                │      │                  │
   │  subprocess.Popen rerun                      │      ▼                  │
   │  SIGTERM stop                                │    SQLite (default)     │
   └──ack + heartbeat────────────────────────────┤      or PostgreSQL       │
                                                  │                         │
                                                  │  GET /api/sse  ─────────┘
                                                  │  (multiplexed SSE,
                                                  │   one connection per page)
                                                  │
                                                  │  retention sweep
                                                  │  email worker
                                                  │  guardrail watchdogs
```

The SDK and backend communicate via schema v1.1 events. The agent
endpoints under `/api/agents/*` are how the host-side `argus-agent`
daemon registers, polls for rerun/stop work, and acknowledges
completed commands.

## Three layers

### 1. SDK — `client/argus/`

A thin Python wrapper around HTTP. Two context-manager classes
(`Reporter`, `JobContext`) plus three daemon threads (heartbeat,
stop-poller, resource-sampler). Idempotent ingest via UUID `event_id`; on
outage, events spill to JSONL files for replay on next start. Framework
adapters under `integrations/`:

* `lightning.py` — wraps Lightning trainer hooks
* `keras.py` — wraps Keras callback hooks

There is no Hydra adapter in the wheel — Hydra integration ships in Sibyl's
`Monitor` callback.

### 2. Backend — `backend/backend/`

FastAPI app, async SQLAlchemy 2.0 models, Alembic migrations.

| Slice | What's there |
|---|---|
| `api/` | 30+ route modules (auth, oauth, tokens, events, sse_multiplex, batches, jobs, agents, artifacts, dashboard, projects, hosts, admin, admin_config, admin_security, …) |
| `models.py` | ORM: `Batch`, `Job`, `Event`, `ResourceSnapshot`, `User`, `Share`, `SystemConfig`, `Agent`, … |
| `services/` | dashboard, sse_hub, eta, runtime_config, executor, retention, jwt_rotation, … |
| `auth/` | JWT (dual-key rotation), argon2id passwords, `em_live_*` and `ag_live_*` token issuance |
| `notifications/` | watchdog + dispatcher + email worker |
| `db.py` | async session factory, dialect-aware pool defaults |
| `config.py` | Pydantic `Settings` reading the `ARGUS_*` env prefix |

### 3. Frontend — `frontend/`

Vue 3 + TypeScript + Vite. Pinia stores per resource. Pages under
`src/pages/`; components under `src/components/`. Ant Design Vue is
**pinned to exact 4.2.6** (and `@ant-design/icons-vue` to `7.0.1`) to avoid
CSS regressions across patch versions. ECharts via `vue-echarts`. i18n
covers English + Simplified Chinese.

Every page subscribes to `GET /api/sse` once with a list of channels rather
than opening N+2 connections. This keeps the proxy / browser connection
budget under control.

## Data flow on a typical run

1. `with Reporter(...)` → `POST /api/events` `batch_start` → backend writes
   a `Batch` row, opens SSE channels, returns 200.
2. `with r.job(...)` → `job_start` event → `Job` row.
3. `job.epoch(0, ...)` → `job_epoch` event → `Event` row + dispatched on the
   relevant SSE channel; the dashboard's loss chart updates.
4. Resource snapshots stream every 30 s (default).
5. On clean exit, `job_done` carries final `metrics` and elapsed time;
   `batch_done` closes the batch with totals. Notification worker decides
   whether to email anyone.

## Idempotency & resilience

* Every event carries a UUID `event_id`; backend dedupes.
* On 5xx / network errors, the SDK worker retries with backoff; persistent
  failures spill to `~/.argus-reporter/*.jsonl`.
* Spill replay happens on next `Reporter` start — even from a different
  process.
* Backend singleton tasks (retention sweeper, email worker) coordinate so
  multiple uvicorn workers don't trip over each other.

## Auth

* **Users**: argon2id passwords + JWT sessions, with **dual-key rotation**.
  Rotating a key (`POST /api/admin/jwt/rotate`) issues a new active `kid`
  while keeping recent keys verify-only — live sessions don't get logged
  out.
* **SDK tokens**: `em_live_*` prefix; bound to a user; argon2-hashed at rest.
* **Agent tokens**: `ag_live_*` prefix; minted by `POST /api/agents/register`
  using an `em_live_*` parent token; bound to the same user.
* **GitHub OAuth**: optional federated login at
  `/api/oauth/github/start` → callback at `/api/oauth/github/callback`.

## Realtime

A single SSE endpoint, `GET /api/sse`, multiplexes named channels. The
client subscribes by passing the desired channel set; the server streams
events tagged with channel name. Reconnects use `Last-Event-ID` so missed
messages are replayed.

## Where to look in the code

| Area | Path |
|---|---|
| HTTP routes | `backend/backend/api/` |
| Domain models | `backend/backend/models.py` |
| Event ingest pipeline | `backend/backend/api/events.py` |
| SSE | `backend/backend/api/sse_multiplex.py`, `services/sse_hub.py` |
| JWT rotation | `backend/backend/api/admin_security.py`, `services/jwt_rotation.py` |
| Executor server side | `backend/backend/api/agents.py`, `services/executor.py` |
| Reporter SDK | `client/argus/context.py`, `client/argus/reporter.py` |
| Vue pages | `frontend/src/pages/` |

## See also

* [Argus Agent](ops/argus-agent.md) — host-side companion (in Sibyl).
* [Database](ops/database.md) — SQLite vs PostgreSQL.
