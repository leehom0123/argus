> 🌐 **English** · [中文](./scaling.zh-CN.md)

# Scaling the backend

Argus runs on a single uvicorn worker by default. That is
enough for a small team reporting a few thousand events per day onto
SQLite. As ingest grows, three levers unlock more throughput without any
migration: **uvicorn workers**, **DB connection pool**, and **reporter
SDK batching** (already on — see §6).

This page documents the defaults shipped in `deploy/entrypoint.sh` +
`deploy/docker-compose.yml`, the process-level singletons that sit
behind them, and when to scale further.

## 1. Uvicorn workers

The container entrypoint launches uvicorn with `--workers 4` by
default. Each worker is a separate OS process. They share the port via
`SO_REUSEPORT`, so requests round-robin across them without any
external load balancer.

Override per-deploy:

```bash
# deploy/.env
ARGUS_WORKERS=2      # small VM
ARGUS_WORKERS=8      # 8-core host running Postgres
```

| Workers | DB backend | Comment                                             |
|---------|------------|-----------------------------------------------------|
| 1       | SQLite     | Dev loop. SQLite is a single-writer DB.             |
| 1–2     | SQLite     | Up to ~5 concurrent reporters; same constraint.     |
| 4       | Postgres   | Default. Handles ~25 RPS on a 4-core box.           |
| `N_CORES` | Postgres | Ingest-heavy; pair with the pool math in §2.        |

The healthcheck, CORS, SPA fallback and event ingest are stateless, so
they scale linearly. The pieces that don't — see §3.

## 2. DB connection pool

Each worker opens its own SQLAlchemy pool. The compose file surfaces
two knobs:

```yaml
ARGUS_DB_POOL_SIZE: 10        # steady-state sockets per worker
ARGUS_DB_POOL_MAX_OVERFLOW: 15 # burst allowance per worker
```

Total peak connections to the DB = `workers × (pool_size + max_overflow)`.
With the shipped defaults that is `4 × 25 = 100`, which exactly matches
PostgreSQL's default `max_connections = 100`.

If you add `pgbouncer` in transaction-pooling mode upstream of the
backend, drop both values to `5` — the bouncer amortises real backend
connections for you.

Unset both to fall through to `backend.db._pool_defaults_for(dialect)`:

- **SQLite**: `pool_size=1`, no overflow (single-writer constraint).
- **PostgreSQL**: `pool_size=20`, `max_overflow=30`.

## 3. Process-level singletons

Some loops are **not** safe to run in every worker. We guard them with
`fcntl.flock` against `/tmp/em-<name>.lock`. The first worker to boot
wins the lock; the rest short-circuit out of the startup block. The
kernel drops the flock automatically on process exit (even SIGKILL), so
a crashed worker is replaced without operator action.

| Loop                     | Singleton? | Why                                                              |
|--------------------------|-----------:|------------------------------------------------------------------|
| JWT blacklist purge      | no         | In-memory state, per-process; must run everywhere.               |
| Watchdog rules           | yes        | Hits every row in `event` / `batch` — parallel sweeps race.      |
| SQLite backup cron       | yes        | Writes one file per invocation. 4 workers → 4 dupes per hour.    |
| Retention sweeper        | yes        | `DELETE ... WHERE timestamp < cutoff`. Parallel sweeps deadlock. |

The retention loop is new in this release. Previously the sweeper only
ran via the admin `POST /api/admin/retention/sweep` endpoint. It now
runs every `ARGUS_RETENTION_SWEEP_MINUTES` (default 60). Set to `0`
to disable and keep driving it from an external cron.

## 4. What about SSE?

The `/api/events/stream` endpoint is served by whichever worker the
client landed on. SSE streams are long-lived, so a single worker can
hold a few thousand open clients. With 4 workers on a 4-core box we
measured ~6k concurrent connections before the Python GIL became the
bottleneck.

Planned follow-up (Team FE): the UI will move most live panels to a
10s HTTP polling loop and keep SSE only for the notification bell and
the running-batch timeline. That reshapes the scaling curve toward
`workers × cache_hit_rate` instead of `workers × open_sockets`. No
backend change required for that transition.

## 5. When to scale further

Shipping these defaults buys you ~50 RPS steady, ~200 RPS burst on a
4-core Postgres host. Beyond that, in order:

1. **Run pgbouncer** in transaction pooling mode. Drops connection
   churn by ~10×, lets you raise `ARGUS_WORKERS` without hitting
   `max_connections`.
2. **Split ingest from dashboard**. Two compose services, same image,
   different nginx upstreams: one tuned for `/api/events/*`, one for
   the read-heavy dashboard queries.
3. **Postgres read replicas**. Point the dashboard service at the
   replica via a second `ARGUS_DB_URL` — requires a small
   `backend.db` patch to pick the engine per-request, not shipped.

## 6. Reporter SDK batching (informational)

The `argus` Python SDK already batches automatically:

- Single events → `POST /api/events`.
- Queue ≥ 20 pending events → batched `POST /api/events/batch` (cap 500 per request).
- Spilled JSONL files on restart → batched replay.

You get this for free; no SDK config required. Do **not** wrap the SDK
in your own debounce layer — doing so will break the spill / replay
guarantees on crash.

## Related

- [PostgreSQL deployment](./postgres.md) — when and how to switch DB backends.
- `backend/backend/retention.py` — what the retention loop actually deletes.
- `backend/backend/app.py::_try_singleton_lock` — the fcntl helper.
