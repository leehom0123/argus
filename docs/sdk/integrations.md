# Framework integrations

Argus ships drop-in callbacks for the major training-loop frameworks so
research users get auto-reporting from their existing `Trainer` /
`model.fit()` flows without writing any boilerplate.

All adapters live under `argus.integrations.*` and use **lazy imports**
— the rest of the SDK is unaffected when a given framework isn't
installed.

| Framework             | Module                              | Install                                    |
| --------------------- | ----------------------------------- | ------------------------------------------ |
| PyTorch Lightning ≥2.0 | `argus.integrations.lightning`      | `pip install 'argus-reporter[lightning]'`  |
| Keras 2 (tf.keras)    | `argus.integrations.keras`          | `pip install 'argus-reporter[keras]'`      |
| Keras 3 (multi-backend) | `argus.integrations.keras`        | `pip install 'argus-reporter[keras]'`      |
| Hydra ≥1.3            | `argus.integrations.hydra`          | `pip install 'argus-reporter[hydra]'`      |

For everything in one go: `pip install 'argus-reporter[all-integrations]'`.

The legacy `Reporter` / `JobContext` API (see `README.md`) is unchanged —
adapters are an additive layer.

## PyTorch Lightning

> **Token security**: Use the `ARGUS_TOKEN` environment variable. Never hardcode tokens in committed code.
>
> ```bash
> export ARGUS_TOKEN=em_live_...
> export ARGUS_URL=https://argus.example.com
> ```
>
> The callback reads these automatically when `token=` and `argus_url=` are omitted.

```python
import pytorch_lightning as pl
from argus.integrations.lightning import ArgusCallback

trainer = pl.Trainer(
    max_epochs=50,
    callbacks=[ArgusCallback(
        project="deepts",
        job_id="etth1_dlinear",
        # argus_url / token omitted -> read from env (recommended)
        final_metric_keys=("val_loss", "MSE", "MAE"),
    )],
)
trainer.fit(model, datamodule)
```

Hook coverage:

* `on_train_start` → `batch_start` + `job_start`
* `on_train_epoch_end` → `job_epoch` (`train_loss`, `val_loss`, `lr`)
* `on_validation_epoch_end` → `job_epoch` (val-only, deduped against the train hook)
* `on_train_end` → `job_done` + `batch_done`
* `on_exception` → `job_failed` + `batch_failed`

The adapter reads metrics from `trainer.callback_metrics` and the LR
from the first optimizer's first param group. Sanity-check validation
runs are skipped automatically.

Tested with Lightning 2.0+. The `lightning` (Lightning 2.x) and
`pytorch_lightning` (long-form alias) namespaces are both auto-detected.

## Keras

> **Token security**: Use the `ARGUS_TOKEN` environment variable. Never hardcode tokens in committed code.
>
> ```bash
> export ARGUS_TOKEN=em_live_...
> export ARGUS_URL=https://argus.example.com
> ```
>
> The callback reads these automatically when `token=` and `argus_url=` are omitted.

```python
from argus.integrations.keras import ArgusCallback

cb = ArgusCallback(
    project="vision",
    job_id="mnist_cnn",
    # argus_url / token omitted -> read from env (recommended)
    final_metric_keys=("val_loss", "val_accuracy"),
)
try:
    model.fit(x, y, validation_data=(xv, yv), epochs=10, callbacks=[cb])
except Exception as exc:
    cb.report_failure(exc)
    raise
```

Hook coverage:

* `on_train_begin` → `batch_start` + `job_start`
* `on_epoch_end` → `job_epoch` (`loss`, `val_loss`, `lr`, plus all numeric extras from `logs`)
* `on_train_end` → `job_done` + `batch_done`
* `report_failure(exc)` → `job_failed` + `batch_failed` (call from your `except` branch)

Keras has no native `on_exception` hook, so failure reporting is
opt-in via `report_failure`. As a safety net, the callback's destructor
emits `job_failed` if `on_train_end` was never reached, but the
diagnostic message in that path is generic — wrapping `fit()` in
`try/except` and calling `report_failure(exc)` gives accurate
exception types and tracebacks.

The adapter prefers the standalone `keras` package (Keras 3) and falls
back to `tensorflow.keras` (Keras 2) when only TensorFlow is available.

## Hydra

> **Token security**: Use the `ARGUS_TOKEN` environment variable. Never hardcode tokens in committed code.
>
> ```bash
> export ARGUS_TOKEN=em_live_...
> export ARGUS_URL=https://argus.example.com
> ```
>
> The callback reads these automatically when `token=` and `argus_url=` are omitted.

```yaml
# configs/config.yaml — wire it in once, every run reports automatically.
hydra:
  callbacks:
    argus:
      _target_: argus.integrations.hydra.ArgusCallback
      project: deepts
      experiment_type: forecast
```

```python
# main.py — unchanged.
import hydra

@hydra.main(version_base=None, config_path="configs", config_name="config")
def main(cfg):
    train(cfg)

if __name__ == "__main__":
    main()
```

Hook coverage:

* `on_run_start` (single-run) / `on_multirun_start` (multirun) → `batch_start`
* `on_job_start` → `job_start` (job_id from `HydraConfig.job.num`)
* `on_job_end` → `job_done` (or `job_failed` when `JobReturn.status == FAILED`)
* `on_run_end` (single-run) / `on_multirun_end` (multirun) → `batch_done`

The callback adapts to both modes automatically:

* `python main.py` — emits **1 batch with 1 job**.
* `python main.py -m experiment=a,b,c` — emits **1 batch with N jobs**, one per Hydra trial.

The reporter's `batch_id` is published to the global slot on enter, so
module-level `argus.emit(...)` works deep inside training code without a
Reporter handle:

```python
from argus import emit
emit("log_line", line="loaded 32 batches", level="info")
```

Tested with hydra-core 1.3.x and 1.4+. The
`hydra.experimental.callback.Callback` base has been stable since the
callback feature shipped in 1.1.

### Custom `job_id`

Default is `str(HydraConfig.job.num)` (`"0"`, `"1"`, …). Override via:

* `job_id_key="experiment_name"` — pulls `cfg.experiment_name` for each job.
* `job_id_template="{experiment_name}-{job_num}"` — `str.format` with all
  scalar config fields plus `job_num`.

```yaml
hydra:
  callbacks:
    argus:
      _target_: argus.integrations.hydra.ArgusCallback
      project: deepts
      job_id_template: "{experiment_name}-{job_num}"
```

## Constructor reference

Lightning + Keras `ArgusCallback` share these kwargs:

| Kwarg                   | Default        | Notes                                                          |
| ----------------------- | -------------- | -------------------------------------------------------------- |
| `project`               | required       | Argus `source_project` (logical group / repo)                  |
| `job_id`                | required       | Identifier for this run                                        |
| `argus_url`             | env `ARGUS_URL`| Server base URL                                                |
| `token`                 | env `ARGUS_TOKEN` | Bearer token                                                |
| `model`, `dataset`      | `None`         | Forwarded into `job_start`                                     |
| `experiment_type`       | `"lightning"` / `"keras"` | Surfaced on the dashboard                          |
| `train_loss_keys`       | framework default | Ordered fallback list for the train-loss metric             |
| `val_loss_keys`         | framework default | Same, for val-loss                                          |
| `final_metric_keys`     | `()`           | Stashed via `JobContext.metrics()` at clean exit               |
| `heartbeat`             | `False`        | Daemon — set `True` or a numeric interval to enable            |
| `stop_polling`          | `False`        | Same                                                           |
| `resource_snapshot`     | `False`        | Same                                                           |
| `auto_upload_dirs`      | `None`         | Directories whose images/PDFs upload as batch artifacts        |

Daemons default off so out-of-the-box overhead is negligible. Enable
them when running long jobs you want the platform to monitor live.

The Hydra `ArgusCallback` does **not** take `job_id`/`final_metric_keys`/
`train_loss_keys`/`val_loss_keys` — those are owned by the inner training
loop, not the Hydra layer. Its specific kwargs:

| Kwarg                   | Default                       | Notes                                          |
| ----------------------- | ----------------------------- | ---------------------------------------------- |
| `project`               | required                      | Argus `source_project`                         |
| `experiment_type`       | `"hydra"`                     | Surfaced on the dashboard                      |
| `argus_url`, `token`    | env                           | As above                                       |
| `batch_prefix`          | `cfg.experiment_name` or `project` | Prefix for the auto-generated batch id    |
| `job_id_key`            | `None`                        | Pull `job_id` from this config field           |
| `job_id_template`       | `None`                        | `str.format` template; `{job_num}` + scalar config keys |
| `heartbeat`, `stop_polling`, `resource_snapshot` | `False` | As above                              |
| `auto_upload_dirs`      | `None`                        | As above                                       |
