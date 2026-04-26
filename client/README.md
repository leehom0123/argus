> 🌐 **English** · [中文](./README.zh-CN.md)

# argus-reporter

Fire-and-forget Python client for the [Argus](../) service.
Speaks **event schema v1.1** (adds client-generated `event_id` for idempotency).

Training runs push lifecycle events (`batch_start`, `job_epoch`, `job_done`, ...)
to a central monitor over HTTP. The client is designed to never raise into
training code and never block the training loop. If the backend is down, events
are spilled to a JSONL file and replayed on the next run — and because every
event carries a UUID `event_id`, replays are safe even if some of them already
reached the backend (it deduplicates by id).

## Install

Three supported paths, pick whichever matches your setup:

```bash
# 1) From PyPI (recommended for connected machines)
pip install argus-reporter

# 2) Pre-built wheel shipped in this repo (air-gapped / offline installs)
pip install client/dist/argus-<version>-py3-none-any.whl

# 3) From source during development
pip install -e "client/[dev]"
```

The wheel at `client/dist/*.whl` (and the copy vendored in downstream
projects such as `DeepTS-Flow-Wheat/tools/wheels/`) exists as an
**offline fallback** for servers without PyPI access. On a connected
machine, prefer the PyPI path — it picks up patch releases automatically
and avoids managing the wheel file manually.

Runtime dependency is `requests>=2.31`. Nothing else.

## Quick start

```python
import os
from argus import Reporter

os.environ.setdefault("ARGUS_URL", "http://argus.local:8000")
os.environ.setdefault("ARGUS_TOKEN", "em_live_...")

with Reporter(
    "etth1-sweep",
    experiment_type="forecast",
    source_project="DeepTS-Flow-Wheat",
    n_total=2,
    command="scripts/forecast/run_benchmark.py --epochs 50",
) as r:
    for job_id, model in [("j1", "transformer"), ("j2", "patchtst")]:
        with r.job(job_id, model=model, dataset="etth1") as j:
            for epoch in range(50):
                j.epoch(epoch, train_loss=tl, val_loss=vl, lr=lr)
                if j.stopped:           # platform stop button hit
                    break
            j.metrics({"MSE": 0.44, "MAE": 0.42})
            j.upload("outputs/.../visualizations")  # PNG/PDF artifacts
```

The keys you pass to `j.metrics({...})` are entirely user-defined —
Argus stores the dict as-is and surfaces every key as a leaderboard
column. `MSE` / `MAE` above are the time-series forecasting
convention; pick whatever names make sense for your task. Step-by-step
guide: [How-to: report metrics for the
leaderboard](../docs/how-to/report-metrics-for-leaderboard.md).

What that gives you for free, compared to the old 4-script setup:

* `batch_start` / `batch_done` (or `batch_failed` on exception) emitted automatically.
* `job_start` / `job_done` / `job_failed` per job.
* Daemon thread emitting `heartbeat` every 5 min so long analysis callbacks
  don't trip the stalled-batch detector.
* Daemon thread polling `/api/batches/<id>/stop-requested` every 10 s; expose
  the result via `r.stopped` / `j.stopped`.
* Daemon thread sampling per-process GPU / CPU / RAM / disk every 30 s and
  emitting `resource_snapshot`.
* `j.upload(...)` posting PNG / PDF / SVG visualizations as job artifacts.

Each daemon takes a `True` / `False` / numeric override:

```python
with Reporter(..., heartbeat=60, stop_polling=False, resource_snapshot=15) as r:
    ...
```

`ARGUS_URL` / `ARGUS_TOKEN` may also be passed explicitly via
`monitor_url=` / `token=`. When neither env nor argument is set the
SDK warns once and degrades to no-ops — your training never crashes.

### Low-level API (v0.1.x — still supported)

```python
from argus import ExperimentReporter

rep = ExperimentReporter(url="http://monitor.local:8000", project="DeepTS-Flow-Wheat")
rep.batch_start(experiment_type="forecast", n_total=1)
rep.job_start(job_id="j1", model="transformer", dataset="etth1")
rep.job_done(job_id="j1", metrics={"MSE": 0.44}, elapsed_s=12.3)
rep.batch_done(n_done=1, n_failed=0)
rep.close()
```

## Integration patterns

### 1. Wrap a benchmark driver (central emitter)

Open one Reporter in the orchestrator, have it call `batch_start` once, and
emit `job_start` / `job_done` as each subprocess training completes. No
changes required inside the training script. See
[`examples/benchmark_wrapper.py`](examples/benchmark_wrapper.py) and the
`scripts/forecast/run_benchmark.py` pattern in DeepTS-Flow-Wheat.

### 2. As a training callback

If your framework supports callbacks, wire the Reporter into `on_train_begin`
/ `on_epoch_end` / `on_train_end`. See
[`examples/callback_style.py`](examples/callback_style.py).

### 3. Mix of both

The orchestrator emits batch-level events; each training subprocess creates
its own Reporter with the shared `batch_id` and emits its own job-level
events. `batch_id` threading is the only coordination needed.

## Failure modes and guarantees

| Situation | Behavior |
|-----------|----------|
| Backend unreachable / DNS fail | POST retried 3× (100 ms → 300 ms → 1 s), then event appended to spill file. Caller never sees the failure. |
| Backend returns 5xx | Same retry-then-spill path. |
| Request times out (default 10 s) | Same. |
| Backend returns 429 | Sleep for `Retry-After` (capped at 60 s), then retry. Not counted against the 3-retry budget. |
| Backend returns 401 / 403 | Log `error("Invalid credentials")`, drop event. No retry. |
| Backend returns 415 / 422 | Log `error("Schema mismatch")`, drop event. No retry. |
| Backend returns 404 | Log `warning`, drop event. |
| Queue full (default 1000 events) | Drop oldest, log a warning. |
| Invalid event (missing `batch_id` / `event_id`, unknown `event_type`) | Dropped at enqueue with warning. Never posted. |
| Process exits abnormally | Daemon thread attempts drain via `atexit`. Anything not drained stays in the queue — only persisted items are those already spilled. |
| Next process startup | Worker scans `~/.argus-reporter/*.jsonl` in mtime order and replays each file via `POST /api/events/batch`. Successful files are deleted. |

**The caller's training run is sacred.** All reporter code paths are
wrapped in `try` / `except` — exceptions are logged via
`logging.getLogger("argus")`, never raised.

## Configuration reference

### `Reporter(...)`

| Argument | Default | Description |
|----------|---------|-------------|
| `batch_prefix` | `"batch"` | Prefix for the auto-generated batch id (`"<prefix>-<12 hex>"`). |
| `experiment_type` | `"experiment"` | Forwarded into the `batch_start` event. |
| `source_project` | `"default"` | Logical project name. |
| `command` | `None` | Reproducibility hint surfaced on `BatchDetail`. |
| `n_total` | `0` | Expected job count; drives the progress bar. |
| `heartbeat` | `True` (300 s) | `True` / `False` / numeric override (seconds). Daemon emits a `log_line` heartbeat so long analysis callbacks don't trip the stalled-batch detector. |
| `stop_polling` | `True` (10 s) | Polls `GET /api/batches/<id>/stop-requested`; flips `r.stopped` / `j.stopped` so the loop can exit cleanly. |
| `resource_snapshot` | `True` (30 s) | Emits per-process GPU / CPU / RAM / disk `resource_snapshot` events. |
| `monitor_url` | env `ARGUS_URL` / `configs/monitor.yaml` | Falls through env then optional yaml. |
| `token` | env `ARGUS_TOKEN` | Bearer token. |
| `auto_upload_dirs` | `None` | Iterable of dirs whose `{.png,.jpg,.pdf,.svg}` files are uploaded as batch artifacts on clean exit. |

If no `monitor_url` resolves the SDK warns once and degrades to no-ops —
training never crashes.

### `ExperimentReporter(...)` (v0.1.x — low-level)

| Argument | Default | Description |
|----------|---------|-------------|
| `url` | (required) | Base URL of monitor service. `/api/events` is appended. |
| `project` | (required) | Logical project name (e.g. `"DeepTS-Flow-Wheat"`). |
| `host` | `socket.gethostname()` | Reporting host. |
| `user` | `$USER` / `$USERNAME` | Reporting user. |
| `commit` | `git rev-parse --short HEAD` | Git SHA of the code. Auto-detected; `None` if outside a git repo. |
| `auth_token` | `None` | Sent as `Authorization: Bearer <token>` if provided. |
| `timeout` | `10.0` | Per-request HTTP timeout in seconds. |
| `queue_size` | `1000` | Bounded internal queue. Drop-oldest policy once full. |
| `spill_path` | `~/.argus-reporter/spill-<pid>-<ts>.jsonl` | Fallback JSONL for undeliverable events. |
| `batch_id` | auto UUID on first `batch_start()` | Override to share one batch across multiple Reporter instances (e.g., orchestrator + training subprocess). |

## Environment variables

| Variable | Effect |
|----------|--------|
| `ARGUS_URL` | Default monitor base URL for `Reporter(...)`. |
| `ARGUS_TOKEN` | Default Bearer token. |
| `ARGUS_DISABLE=1` | Every method becomes a no-op. No network traffic, no spill file, no worker thread. Perfect for researchers who don't run Argus locally. Also accepts `true`, `yes`, `on`. |

## Event schema (v1.1)

All events match [`schemas/event_v1.json`](../schemas/event_v1.json).
`schema_version` is pinned to `"1.1"` in this release. Every event also
carries a client-generated UUID `event_id` that the backend uses to
deduplicate retried or replayed POSTs.

Supported `event_type` values:

- `batch_start`, `batch_done`, `batch_failed` — sweep lifecycle
- `job_start`, `job_epoch`, `job_done`, `job_failed` — per-training-run
- `resource_snapshot` — host-level gauges (GPU / RAM / disk)
- `log_line` — optional log forwarding

Extra kwargs passed to any method are merged into the event's `data` field,
so forward-compat is preserved: add a `run_dir="..."` or `config_digest="..."`
field without needing a client release.

## Transport

- Single event per call: `POST {url}/api/events` with `Authorization: Bearer <token>`.
- Bursts or spill replay: `POST {url}/api/events/batch` with body
  `{"events": [...]}` (up to 500 events per batch).
- Both endpoints accept and respond with `application/json`.
- Retries: 5xx and network errors retry 3× with exponential backoff (100 ms,
  300 ms, 1 s). 429 waits for `Retry-After` (capped). 4xx other than 429
  drop the event with a structured log line.

## Idempotency (new in v1.1)

Each event is stamped with a UUID4 `event_id` at build time. The backend's
`POST /api/events[/batch]` handler deduplicates by this id: if the same
event comes in twice (because we retried after a timeout, or because a
spilled event survived a crash and the original POST secretly succeeded),
the backend returns 200 with the original `db_id` and does **not** create
a duplicate row. That means the client's at-least-once delivery model
composes cleanly with exactly-once storage.

## Testing

```bash
pip install -e ".[dev]"
pytest -q
```

Tests use `pytest-httpserver` for a local mock backend and `jsonschema` to
validate every emitted event against `schemas/event_v1.json`.

## License

MIT (see project root).
