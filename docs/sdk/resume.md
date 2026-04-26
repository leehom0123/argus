# Batch identity & crash-resume

A long-running benchmark (10 datasets × 12 models, days of GPU time) is
the most likely thing in your life to crash on a Tuesday afternoon.
The `argus` SDK lets you **resume the same logical batch** after a
crash so the partial results stay co-located on the backend instead of
fragmenting across two distinct Batch rows.

This page covers the three usage modes, the on-the-wire semantics, and
the two backend guarantees that make resume safe.

## Three usage modes

### 1. Auto-derived (recommended default)

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
              batch_id=batch_id) as r:
    ...
```

`derive_batch_id` hashes `(project, experiment_name, git_sha)` into a
deterministic `bench-<16 hex>` id. Re-running the same launcher from
the same checkout produces the **same** id, so events from the resumed
run land on the existing Batch row on the backend.

If you switch git commits between runs the id changes, which is what
you want — a different commit really is a different experiment.

### 2. Explicit pinning

```python
with Reporter(source_project="demo",
              n_total=4,
              batch_id="my-paper-table-1-bench") as r:
    ...
```

Use this when you need a human-readable id, or when you want to share
one batch across multiple launcher invocations (e.g. a CI job that
splits work across machines).

### 3. Resume after crash

```python
with Reporter(source_project="demo",
              n_total=120,
              resume_from="bench-abcdef0123456789") as r:
    ...
```

`resume_from` is an alias for `batch_id` — same wire effect, but the
keyword carries the intent in your code. Pass the id printed by your
crashed launcher (or recovered from the backend's `/batches` list) and
all subsequent events append to the original Batch.

`batch_id` wins over `resume_from` if you accidentally pass both.

## End-to-end crash + resume walkthrough

```python
# Tuesday 09:00 — first launch.
batch_id = derive_batch_id("my-bench", "dam_forecast")
print(f"running batch {batch_id}")  # bench-abcdef0123456789

with Reporter(source_project="my-bench",
              experiment_type="forecast",
              n_total=120,
              batch_id=batch_id) as r:
    for job in plan.jobs:
        with r.job(job.id, model=job.model, dataset=job.dataset) as j:
            train_one(j)
        # ↑ 47 jobs in, machine reboots.
```

The launcher posts `batch_start` for `bench-abcdef0123456789`, completes
47 jobs, then dies. The backend's Batch row stays at `status=running`
because no `batch_done` was emitted — the `_handle_batch_start`
side-effect handler (see
[`backend/api/events.py`](https://github.com/argus-ai/argus/blob/main/backend/backend/api/events.py))
preserves the original `start_time` for resume.

```python
# Tuesday 14:00 — resumed launch.
batch_id = derive_batch_id("my-bench", "dam_forecast")
# Same checkout → same id (no need to write it down).
with Reporter(source_project="my-bench",
              experiment_type="forecast",
              n_total=120,
              resume_from=batch_id) as r:
    for job in plan.remaining_jobs():  # the 73 we didn't get to
        with r.job(job.id, model=job.model, dataset=job.dataset) as j:
            train_one(j)
```

The second launch posts `batch_start` for the same id. The backend
recognises the existing row, refreshes mutable fields (latest command,
latest `n_total`), flips `status` back to `running`, and **leaves the
original `start_time` alone**. Every job event from the resumed run
appends to the existing batch — your `/batches/<id>/jobs` list ends up
with all 120 entries on one row.

## Backend guarantees

The resume contract relies on three idempotency properties of the
`POST /api/events` ingest path:

1. **`batch_start` for an existing batch_id is accepted, not 409'd.**
   The handler updates mutable metadata (command, n_total, project)
   and returns 200 + `accepted=true`.

2. **The original `start_time` is preserved on re-init.** Subsequent
   `batch_start` events refresh state but never bump the start
   timestamp — historical timing stays intact across resumes.

3. **A previously-`done` batch is left alone.** Idempotent re-run
   safety: re-launching a finished batch does not flip its status
   back to `running`. Pass a different id if you really want to
   re-execute.

The matching backend tests live in
[`test_resume_appends_to_existing_batch.py`](https://github.com/argus-ai/argus/blob/main/backend/backend/tests/test_resume_appends_to_existing_batch.py).

## Output-dir conventions in launchers

Argus only stores the metadata; what each launcher writes to disk is
its own concern. The recommended convention (used by the sibyl
forecast pipeline) is a two-layer subtree under `outputs/<task>/`:

```
outputs/forecast/<batch_id>/
├── <experiment_1>/
│   ├── checkpoints/
│   ├── leaderboard.csv
│   └── COMPLETED
├── <experiment_2>/
└── ...
```

Resuming with the same `batch_id` reuses the same parent directory, so
file-level resume mechanisms (e.g. `checkpoint_last.pt`) and the
on-disk leaderboard continue to work without migration.

## Reference

`derive_batch_id` is exported from the package root:

::: argus.derive_batch_id
    options:
      show_signature: true
      show_root_heading: true

The `Reporter` `batch_id` / `resume_from` kwargs are documented in
[`reporter.md`](reporter.md).
