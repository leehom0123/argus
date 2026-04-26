# Reporter API

The `argus` package (PyPI: `argus-reporter`) is the Python SDK for pushing
events into an Argus server. The public surface is two context-manager
classes (`Reporter`, `JobContext`) plus a handful of escape hatches.

## Installation

```bash
pip install argus-reporter
pip install argus-reporter[lightning]         # PyTorch Lightning callback
pip install argus-reporter[keras]             # Keras callback
pip install argus-reporter[hydra]             # Hydra callback
pip install argus-reporter[all-integrations]  # all of the above
```

Python ≥3.10. Only required runtime dependency is `requests`.

## Public API

```python
from argus import (
    Reporter, JobContext,                  # high-level context managers
    derive_batch_id,                       # crash-safe batch id
    emit, new_batch_id, set_batch_id,      # module-level escape hatches
    get_batch_id, sub_env,
    ExperimentReporter,                    # legacy low-level class (still supported)
    SCHEMA_VERSION,                        # the wire-protocol version string
)
```

## Configuration

| Source | Variable / arg | Effect |
|---|---|---|
| Constructor arg | `monitor_url=` | Server URL |
| Env var | `ARGUS_URL` | Same — used when `monitor_url` is omitted |
| File | `configs/monitor.yaml` (`url:` or `monitor_url:`) | Last-resort fallback |
| Constructor arg | `token=` | `em_live_…` SDK token |
| Env var | `ARGUS_TOKEN` | Same — used when `token` is omitted |
| Env var | `ARGUS_DISABLE=1` | Short-circuit the SDK to a no-op |

Spill files for retry-after-outage land in `~/.argus-reporter/*.jsonl`.
The worker scans them on startup and replays in mtime order.

## `Reporter`

Top-level batch context manager. Signature:

```python
Reporter(
    batch_prefix: str = "batch",
    *,
    experiment_type: str | None = None,
    source_project: str | None = None,
    command: str | None = None,
    n_total: int | None = None,
    heartbeat: bool | float = True,         # default True → 300 s
    stop_polling: bool | float = True,      # default True → 10 s
    resource_snapshot: bool | float = True, # default True → 30 s
    monitor_url: str | None = None,
    token: str | None = None,
    auto_upload_dirs: Iterable[str | Path] | None = None,
    batch_id: str | None = None,            # explicit id, overrides batch_prefix
    resume_from: str | None = None,         # alias for batch_id (intent: resume)
)
```

| Argument | Notes |
|---|---|
| `batch_prefix` | Prefix for the auto-generated batch id (`<prefix>-<12 hex>`) |
| `experiment_type` | Forwarded into `batch_start` (e.g. `"forecast"`, `"gene_expr"`) |
| `source_project` | Project namespace; defaults to `"default"` |
| `command` | Recorded for rerun |
| `n_total` | Expected total jobs; surfaced as a progress baseline |
| `heartbeat` / `stop_polling` / `resource_snapshot` | `True` → default interval (300 / 10 / 30 s); `False` → disabled; numeric → custom interval (s) |
| `monitor_url`, `token` | Fall back to env vars / `configs/monitor.yaml` |
| `auto_upload_dirs` | Directories whose `.png/.jpg/.pdf/.svg` files are uploaded as batch artifacts on **clean exit** |
| `batch_id` | Pin an explicit id (e.g. `derive_batch_id(...)`); overrides `batch_prefix` |
| `resume_from` | Alias for `batch_id` — same wire effect, used for the "resume" intent. `batch_id` wins if both are passed. |

Properties: `batch_id` (str), `stopped` (bool — fires when the platform's
**Stop** button is clicked).

Methods:

* `r.job(job_id, *, model=None, dataset=None) -> JobContext`
* `r.emit(event, **fields)` — direct emit, escape hatch for unusual events

On `__enter__` the Reporter:

1. Picks the batch id (explicit `batch_id` / `resume_from`, else
   `new_batch_id(batch_prefix)`).
2. Posts `batch_start` (best-effort; failures are logged but never raise).
3. Spawns up to three daemon threads (heartbeat, stop-poller, resource-snapshotter).
4. The underlying `ExperimentReporter` worker drains any pre-existing spill.

On `__exit__` it posts `batch_done` (or `batch_failed` if the block raised),
joins worker threads with a 2 s timeout per thread, and closes the
underlying queue with a 3 s drain timeout.

## `JobContext`

Created via `r.job(...)`. Signature:

```python
JobContext(parent: Reporter, job_id: str, *, model=None, dataset=None)
```

Properties: `job_id`, `stopped` (delegates to parent).

Methods:

| Method | Effect |
|---|---|
| `job.epoch(epoch, *, train_loss=None, val_loss=None, lr=None, batch_time_ms=None, **extra)` | Emit `job_epoch` |
| `job.metrics(d: dict)` | Stash final metrics; surfaced on `job_done` |
| `job.log(message, level="INFO")` | Emit `log_line` |
| `job.upload(path, *, glob="**/*.png")` | Upload artifacts under a path. For a directory, `glob` selects which files; types outside `{.png,.jpg,.pdf,.svg}` are skipped. |

There is no `.metric()`, `.tag()`, `.fail()`, `.label()`, or
`.log_artifact()` method — failure handling is automatic on exception in
the `with` block.

## Crash-resume (`derive_batch_id`)

```python
from argus import Reporter, derive_batch_id

batch_id = derive_batch_id(
    project="my-bench",
    experiment_name="dam_forecast",
    # git_sha=None → calls `git rev-parse HEAD`; "no-git" if git is absent.
)

with Reporter(batch_prefix="bench",
              source_project="my-bench",
              experiment_type="forecast",
              n_total=120,
              batch_id=batch_id) as r:           # or resume_from=batch_id
    ...
```

`derive_batch_id(project, experiment_name, git_sha=None, *, prefix="bench")`
hashes a stable triple into a deterministic `<prefix>-<16 hex>` id.
Re-running the same launcher from the same checkout produces the **same**
id, so events from the resumed run land on the existing Batch row on the
backend (which is idempotent on `batch_start`).

See [Batch identity & resume](resume.md) for the full walkthrough.

## Idempotency and retry

Every event carries a UUID `event_id`. The backend dedupes by it. On 5xx /
network errors, the underlying worker retries with backoff; persistent
failures spill to `~/.argus-reporter/*.jsonl`. On the next `Reporter` start
(any process), the worker scans the spill directory and replays via
`POST /api/events/batch`.

## Stop signal

The stop-poller calls `GET /api/batches/{id}/stop-requested` every 10 s
(by default). When the user clicks **Stop** in the UI, the poller flips
`r.stopped` to `True`. Check it inside your training loop:

```python
with r.job("run-1") as j:
    for epoch in range(num_epochs):
        if j.stopped:           # delegates to r.stopped
            break
        train_loss = train_one_epoch()
        j.epoch(epoch, train_loss=train_loss)
```

The job context emits `job_done` cleanly on a controlled break; on an
unhandled exception inside the `with`, it emits `job_failed` automatically.

## Disabling

Set `ARGUS_DISABLE=1` in the environment. The underlying `ExperimentReporter`
becomes a no-op for the whole process — the public API still works so user
code does not need to branch.

## Module-level escape hatches

| Symbol | Use |
|---|---|
| `derive_batch_id(project, experiment_name, git_sha=None, *, prefix="bench")` | Deterministic batch id for resume |
| `emit(event, **fields)` | Push a one-off event without a Reporter context (uses the global Reporter, if any) |
| `new_batch_id(prefix="batch")` | Generate a fresh `<prefix>-<12hex>` |
| `set_batch_id(batch_id)` / `get_batch_id()` | Inherit a parent batch id (e.g. for a child process) |
| `sub_env(template, **extra)` | Substitute `${argus_batch_id}` and friends in templated strings |

## Auto-generated reference

::: argus.context.Reporter
    handler: python

::: argus.context.JobContext
    handler: python

::: argus.identity.derive_batch_id
    handler: python

## See also

* [Connect a training job](../getting-started/connect-training.md) — Lightning, Keras, vanilla.
* [Hydra callback](hydra-callback.md) — first-class Hydra adapter.
* [Batch identity & resume](resume.md) — `derive_batch_id` end-to-end.
* [Event schema](event-schema.md) — wire format.
