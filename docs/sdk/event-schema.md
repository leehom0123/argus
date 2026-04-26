# Event schema

All events posted to Argus conform to **schema v1.1**, defined in
[`schemas/event_v1.json`](https://github.com/leehom0123/argus/blob/main/schemas/event_v1.json).
The SDK validates outgoing events against this schema before posting.

## Wire envelope

```json
{
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "schema_version": "1.1",
  "event_type": "job_epoch",
  "timestamp": "2026-04-23T09:23:06Z",
  "batch_id": "bench-2026-04-23-abc123",
  "job_id": "etth1_dlinear",
  "source": {
    "project": "my-paper",
    "host": "node-12",
    "user": "alice@example.com",
    "commit": "a1b2c3d",
    "command": "python train.py model=patchtst"
  },
  "data": { "epoch": 7, "train_loss": 0.32, "val_loss": 0.41 }
}
```

### Required fields

| Field | Type | Notes |
|---|---|---|
| `event_id` | UUID | Idempotency key — backend dedupes on conflict. Repeated POSTs return 200 with the original db_id. |
| `schema_version` | `"1.1"` (const) | Bump major for breaking changes |
| `event_type` | enum (see below) | Discriminator for the `data` payload |
| `timestamp` | ISO 8601 UTC | e.g. `2026-04-23T09:23:06Z` |
| `batch_id` | string (1..128) | Groups jobs from the same sweep / invocation |
| `source.project` | string | Project namespace |

### Optional fields

| Field | Notes |
|---|---|
| `job_id` | Null for batch-level events; **required** (non-null) for `job_start`, `job_epoch`, `job_done`, `job_failed`, `log_line` (the `JOB_SCOPED` set in `client/argus/schema.py`) |
| `source.host`, `source.user`, `source.commit`, `source.command` | Recorded on `batch_start`; carried through downstream |
| `data` | Event-specific payload; extra fields accepted and stored for forward-compat |

## Event types (9)

| `event_type` | When | Typical `data` shape |
|---|---|---|
| `batch_start` | Reporter `__enter__` | `{experiment_type, n_total_jobs, command}` |
| `batch_done` | Clean exit | `{n_done, n_failed, total_elapsed_s}` |
| `batch_failed` | Exception in `with Reporter` | `{reason, total_elapsed_s}` |
| `job_start` | `JobContext.__enter__` | `{model, dataset, ...}` |
| `job_epoch` | `job.epoch(...)` | `{epoch, train_loss, val_loss, lr, batch_time_ms, **extra}` |
| `job_done` | Clean job exit | `{metrics, elapsed_s, train_epochs}` |
| `job_failed` | Exception in `with r.job` | `{reason, elapsed_s}` |
| `resource_snapshot` | Every `resource_snapshot` interval | `{cpu_pct, rss_mb, gpus: [...]}` |
| `log_line` | `job.log(...)` | `{level, line}` |

There is no separate `job_metric` or `batch_heartbeat` event type — final
metrics ride on `job_done`; the heartbeat is implicit (a backend "last seen"
update on every event).

## Schema versioning

* **Major** bumps are wire-breaking — old SDKs are rejected with 4xx until they upgrade.
* **Minor** bumps add optional fields; old clients silently strip them.
* The current Argus accepts schema `1.1`. The next major (2.0) is not on the roadmap.

A new optional field requires a minor bump and an update to
`schemas/event_v1.json`.

## Posting events

```
POST /api/events           # single event
POST /api/events/batch     # array of up to 500 events (used by the SDK for
                           # bursts of >=20 queued events and for spill
                           # replay)
```

The SDK auto-flushes via the batch endpoint when the local queue holds
≥20 pending events (`_BATCH_FLUSH_THRESHOLD` in `client/argus/reporter.py`),
and caps each batch POST at 500 events (`_BATCH_MAX_EVENTS`).

Authorization: `Bearer em_live_…` for SDK tokens, `Bearer ag_live_…` for
agent tokens. The dedupe key is `event_id`. Re-posting an event Argus has
already seen returns `200 OK` with the same `db_id` and no second row.

### HTTP semantics observed by the SDK worker

| Status | Worker behaviour |
|---|---|
| 2xx | accept, drop the event from the queue |
| 401 / 403 | log error, drop the event (bad credentials) |
| 404 | log warning, drop the event |
| 415 / 422 | log error, drop the event (schema mismatch) |
| 429 | honour `Retry-After` (default 30 s, capped 60 s), retry |
| 5xx / network | exponential backoff `100 ms → 300 ms → 1 s`; persistent failures append to `~/.argus-reporter/<batch>.jsonl` for replay on next start |

## Reading

There is **no public read API for raw events** — they are an implementation
detail. Use the higher-level resource APIs:

* `GET /api/batches` — list / filter
* `GET /api/batches/{id}` — detail
* `GET /api/jobs` — flat list
* `GET /api/sse` — multiplexed SSE stream

## See also

* [Reporter API](reporter.md) — what generates these events.
* [Architecture overview](../architecture-overview.md) — where they go after ingest.
