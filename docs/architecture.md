> 🌐 [中文](./architecture.zh-CN.md) · **English**

# Architecture notes

## Data model (three-layer hierarchy)

```
batch   — one sweep/benchmark invocation (e.g. `run_benchmark.py --epochs 50 ...`)
  └── job    — one model×dataset×seed combination inside the batch
        └── event — life-cycle events (start, epoch, done, failed, ...)

resource_snapshot — independent, periodic GPU/CPU/mem timeseries per host
```

## Event schema

Canonical definition: `schemas/event_v1.json` (JSON Schema draft-07).

All events include `schema_version`, `event_type`, `timestamp`, `batch_id`, `source`. `data` payload varies by `event_type`.

## SQLite tables (backend owns)

```sql
-- Groups of jobs from one sweep / benchmark invocation
CREATE TABLE batch (
    id               TEXT PRIMARY KEY,
    experiment_type  TEXT,                -- 'forecast' | 'gene_expr' | ...
    project          TEXT NOT NULL,       -- e.g. 'DeepTS-Flow-Wheat'
    user             TEXT,
    host             TEXT,
    command          TEXT,
    n_total          INTEGER,
    n_done           INTEGER DEFAULT 0,
    n_failed         INTEGER DEFAULT 0,
    status           TEXT,                -- 'running' | 'done' | 'failed'
    start_time       TEXT,                -- ISO 8601
    end_time         TEXT,
    extra            TEXT                 -- JSON
);

-- A single run
CREATE TABLE job (
    id               TEXT,                -- unique within batch
    batch_id         TEXT NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    model            TEXT,
    dataset          TEXT,
    status           TEXT,                -- 'running' | 'done' | 'failed'
    start_time       TEXT,
    end_time         TEXT,
    elapsed_s        INTEGER,
    metrics          TEXT,                -- JSON from job_done
    extra            TEXT,                -- JSON
    PRIMARY KEY (batch_id, id)
);

-- Every raw event; for audit + epoch-level timeseries
CREATE TABLE event (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         TEXT NOT NULL,
    job_id           TEXT,
    event_type       TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    schema_version   TEXT NOT NULL,
    data             TEXT                 -- JSON
);
CREATE INDEX idx_event_batch_job  ON event(batch_id, job_id);
CREATE INDEX idx_event_timestamp  ON event(timestamp);

-- Host resource timeseries, independent of any batch
CREATE TABLE resource_snapshot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    host             TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    gpu_util_pct     REAL,
    gpu_mem_mb       REAL,
    gpu_mem_total_mb REAL,
    gpu_temp_c       REAL,
    cpu_util_pct     REAL,
    ram_mb           REAL,
    ram_total_mb     REAL,
    disk_free_mb     REAL,
    extra            TEXT
);
CREATE INDEX idx_resource_host_ts ON resource_snapshot(host, timestamp);
```

## Consistency rules

- `batch_start` MUST come before any `job_*` with matching `batch_id`.
  - If backend receives `job_*` with unknown `batch_id`, it creates a stub batch row with `status='running'` + what it can glean.
- `job_start` SHOULD come before `job_epoch` / `job_done`.
  - Same stub-creation policy.
- `job_done` and `job_failed` are idempotent: last write wins.
- Counter fields (`batch.n_done`, `batch.n_failed`) are derived — prefer recomputing via SQL over trusting running counters.

## Notification rules (backend-side)

Rules live in `backend/config/notifications.yaml`. Engine evaluates each incoming event against rules. Each matching rule queues a notification to the configured channels.

```yaml
rules:
  - when: event_type == "job_failed"
    push: [feishu, whatsapp]
  - when: event_type == "batch_done" and data.n_failed > 0
    push: [feishu]
  - when: event_type == "resource_snapshot" and data.gpu_util_pct < 10
    push: [feishu]
```

## API endpoints (v1)

```
POST   /api/events                    # ingest, fire-and-forget safe
GET    /api/batches                   # list with ?user=, ?project=, ?status=, ?since=, ?limit=
GET    /api/batches/{batch_id}        # detail with summary stats
GET    /api/batches/{batch_id}/jobs   # jobs in a batch
GET    /api/jobs/{batch_id}/{job_id}  # single job detail + recent events
GET    /api/jobs/{batch_id}/{job_id}/epochs   # loss/metric timeseries
GET    /api/resources                 # ?host=, ?since=, ?limit=
GET    /api/resources/hosts           # distinct hosts seen

GET    /api/events/stream             # SSE stream for live updates (phase 2)
```

## Deployment

Target: a single server (monitor machine or VPS), exposed on :8000.

- `backend/` runs via uvicorn + systemd
- `frontend/dist/` served as static files by the same FastAPI instance
- SQLite database at `backend/data/monitor.db`
- nginx reverse proxy optional for TLS / subdomain
