# Hydra callback

If your experiments are driven by [Hydra](https://hydra.cc/), Argus ships a
first-class drop-in callback at
`argus.integrations.hydra.ArgusCallback`. Wire it into
`hydra.callbacks` and every `@hydra.main` invocation emits a batch
automatically — no per-script SDK boilerplate.

## Install

```bash
pip install 'argus-reporter[hydra]'
```

The `[hydra]` extra pulls in `hydra-core>=1.3`. The import path
(`argus.integrations.hydra`) is lazy — users without Hydra installed are
unaffected by the import.

## Wire it in

```yaml
# configs/config.yaml
defaults:
  - _self_

experiment_name: dam_forecast

hydra:
  callbacks:
    argus:
      _target_: argus.integrations.hydra.ArgusCallback
      project: my-paper
      experiment_type: forecast
```

`main.py` stays unchanged. Set the auth env vars once:

```bash
export ARGUS_URL=https://argus.example.com
export ARGUS_TOKEN=em_live_…
```

That's it — every `python main.py experiment=…` run now emits one batch to
the dashboard.

## Hook coverage

| Hydra hook | Argus event |
|---|---|
| `on_run_start` | `Reporter.__enter__` (single-run mode only) |
| `on_multirun_start` | `Reporter.__enter__` (multirun mode) |
| `on_job_start` | `JobContext.__enter__` (fires for every Hydra job) |
| `on_job_end` | `JobContext.__exit__` (clean or failure based on `job_return.status`) |
| `on_run_end` | `Reporter.__exit__` (single-run mode only) |
| `on_multirun_end` | `Reporter.__exit__` (multirun mode) |

* **Single-run** (`python main.py`) → 1 batch with 1 job. `job_id`
  defaults to `str(HydraConfig.job.num)` (i.e. `"0"`).
* **Multirun** (`python main.py -m a=1,2,3`) → 1 batch with N jobs, one
  per Hydra trial.

The global `batch_id` is set on enter, so module-level `argus.emit(...)`
works without holding a Reporter handle.

## Constructor options

```python
ArgusCallback(
    *,
    project: str,                          # required
    experiment_type: str = "hydra",
    argus_url: str | None = None,          # falls back to ARGUS_URL
    token: str | None = None,              # falls back to ARGUS_TOKEN
    batch_prefix: str | None = None,       # defaults to config.experiment_name or project
    job_id_key: str | None = None,         # config field name to use as job_id
    job_id_template: str | None = None,    # str.format template; overrides job_id_key
    heartbeat: bool | float = False,       # daemons OFF by default — Hydra runs are usually short
    stop_polling: bool | float = False,
    resource_snapshot: bool | float = False,
    auto_upload_dirs: list | None = None,  # uploaded as batch artifacts on clean exit
)
```

`heartbeat`, `stop_polling`, `resource_snapshot` default to **False** in
the Hydra adapter (vs `True` for `Reporter`) because Hydra jobs are often
shorter than the heartbeat interval. Opt back in via the YAML if you want
the live telemetry.

## Custom job ids

Default `job_id` is `str(HydraConfig.job.num)`. Override by either:

```yaml
hydra:
  callbacks:
    argus:
      _target_: argus.integrations.hydra.ArgusCallback
      project: my-paper
      job_id_template: "{experiment_name}-{job_num}"   # → "dam_forecast-0"
      # or:
      # job_id_key: experiment_name                    # uses cfg.experiment_name verbatim
```

`job_id_template` interpolates `{job_num}` plus any field name on the
Hydra config object.

## Inside training code

The callback sets the global batch id, so the module-level `emit` helper
works without you holding a Reporter reference:

```python
from argus import emit

emit("log_line", line="loaded 32 batches", level="info")
```

This is convenient for adding diagnostics deep inside model code without
plumbing a Reporter handle through every function.

## Caveats

* Hydra callbacks run in the parent process. If your sweep launches via
  Submitit / Ray, each worker needs its own Reporter — the parent
  callback only opens the umbrella batch.
* The callback does not handle exceptions raised before `on_run_start`
  fires (e.g. invalid config). Hydra still logs those to disk; they just
  do not show up on the dashboard.

## Studies tab integration

When the Optuna sweeper is active under multirun, you can attach
`optuna.{study_name, trial_number, params_hash}` labels on `job_start`.
Argus's backend stashes them under `Job.extra.optuna`, which lights up
the **Studies** tab automatically. The Sibyl monitor callback
(`sibyl/sibyl/callbacks/monitor.py`) is the canonical example.

## See also

* [Reporter API](reporter.md) — what the callback wraps under the hood.
* [Batch identity & resume](resume.md) — pair the Hydra callback with
  `derive_batch_id` for crash-resume across Hydra restarts.
