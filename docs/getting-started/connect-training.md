# Connect a training job

Once the platform is running and you have an `em_live_…` SDK token, you have
several ways to push events. The vanilla `Reporter` is always the lowest-level
option; framework adapters wrap it.

## Install

```bash
pip install argus-reporter                  # base SDK
pip install argus-reporter[lightning]       # + PyTorch Lightning callback
pip install argus-reporter[keras]           # + Keras callback
pip install argus-reporter[hydra]           # + Hydra callback
pip install argus-reporter[all-integrations]
```

Python ≥3.10. Only required runtime dependency is `requests`.

## Credentials

The SDK reads two env vars by default; you can also pass them as keyword args:

```bash
export ARGUS_URL=https://argus.example.com    # NOTE: ARGUS_URL, not ARGUS_BASE_URL
export ARGUS_TOKEN=em_live_…
```

A spill directory (`~/.argus-reporter/`) is created on first use to buffer
events when the network is unavailable. Spill files are replayed on next
SDK start, even from a different process.

## Vanilla loop

```python
from argus import Reporter

with Reporter("my-run",
              experiment_type="forecast",
              source_project="my-paper",
              n_total=1) as r:
    with r.job("bert-base-lr3e-5", model="bert-base") as j:
        for epoch in range(num_epochs):
            if j.stopped:                       # platform stop button
                break
            train_loss, val_loss = train_one_epoch()
            j.epoch(epoch, train_loss=train_loss, val_loss=val_loss)
        j.metrics({"val_loss": val_loss})       # surfaced on job_done
```

`Reporter` opens a *batch*; each `r.job` opens a *job* inside that batch.
Multiple jobs in one batch is how you record a sweep that shares a single
training script invocation.

## PyTorch Lightning

```python
from argus.integrations.lightning import ArgusCallback
import pytorch_lightning as pl  # or: import lightning as L

trainer = pl.Trainer(callbacks=[ArgusCallback(
    project="my-paper",
    job_id="bert-base",
    model="bert-base-uncased",            # optional metadata on job_start
    dataset="glue/sst2",                  # optional metadata on job_start
    argus_url="http://localhost:8000",    # or rely on ARGUS_URL
    token="em_live_…",                    # or rely on ARGUS_TOKEN

    # Tell the adapter which keys in trainer.callback_metrics map to
    # train_loss / val_loss. The first key that resolves to a finite
    # float wins. Defaults shown:
    train_loss_keys=("train_loss_epoch", "train_loss", "loss"),
    val_loss_keys=("val_loss_epoch", "val_loss"),

    # Final-epoch headline metrics — read once on on_train_end and
    # passed through JobContext.metrics. Anything you self.log() in
    # your LightningModule can land here.
    final_metric_keys=("val_mse", "val_rmse", "val_mae",
                       "val_r2", "val_pcc"),

    # Daemons default OFF for Lightning runs (vs True for raw Reporter).
    heartbeat=False, stop_polling=False, resource_snapshot=False,
)])
trainer.fit(model, datamodule)
```

Constructor (kwargs only, from `client/argus/integrations/lightning.py`):

| Argument | Default | Notes |
|---|---|---|
| `project` | required | `source_project` on `Reporter` |
| `job_id` | required | identifier for this run |
| `model`, `dataset` | None | propagated into `job_start` |
| `argus_url`, `token` | env | fallback to `ARGUS_URL` / `ARGUS_TOKEN` |
| `experiment_type` | `"lightning"` | forwarded to `Reporter` |
| `batch_prefix` | `"lightning"` | prefix for the auto-generated batch id |
| `train_loss_keys` | `("train_loss_epoch", "train_loss", "loss")` | first match wins |
| `val_loss_keys` | `("val_loss_epoch", "val_loss")` | first match wins |
| `final_metric_keys` | `()` | read at `on_train_end` and passed to `JobContext.metrics` |
| `heartbeat`, `stop_polling`, `resource_snapshot` | `False` | opt-in |
| `auto_upload_dirs` | None | folders whose images/PDFs upload on clean exit |

Hook coverage:

| Lightning hook | Argus event |
|---|---|
| `on_train_start` | `Reporter.__enter__` + `JobContext.__enter__` (`batch_start` + `job_start`) |
| `on_train_epoch_end` | `JobContext.epoch` with `train_loss`, `lr` |
| `on_validation_epoch_end` | `JobContext.epoch` with `val_loss` |
| `on_train_end` | clean exit; final metrics flushed via `JobContext.metrics` |
| `on_exception` | failure exit (`job_failed` + `batch_failed`) |

The two epoch hooks are dedup'd against each other (Lightning 2.x fires
`on_validation_epoch_end` before `on_train_epoch_end` within the same
epoch — the adapter emits exactly one `job_epoch` per epoch). Works on
Lightning 1.9+ and 2.x — both the `pytorch_lightning` and
`lightning.pytorch` namespaces are tried.

## Keras

```python
from argus.integrations.keras import ArgusCallback

cb = ArgusCallback(
    project="my-paper",
    job_id="mnist_cnn",
    model="cnn-32-32-64",
    dataset="mnist",
    argus_url="http://localhost:8000",
    token="em_live_…",
    train_loss_keys=("loss",),                       # default
    val_loss_keys=("val_loss",),                     # default
    final_metric_keys=("accuracy", "val_accuracy"),  # read from last-epoch logs
)
model.fit(x, y, epochs=10, callbacks=[cb])
```

Constructor mirrors the Lightning shape; the only differences are
`experiment_type` / `batch_prefix` defaults (`"keras"`) and the per-epoch
keys, which read from the per-epoch `logs` dict Keras passes to
`on_epoch_end`.

Hook coverage: `on_train_begin` opens the Reporter and JobContext;
`on_epoch_end` emits one `job_epoch`; `on_train_end` flushes final
metrics and closes both contexts cleanly. Keras has no
`on_exception`; the callback's destructor best-effort emits a failure
event, and you can call `cb.report_failure(exc)` from a `try/except` for
deterministic reporting.

Works with both Keras 3 (`import keras`) and the TF-bundled Keras 2.

## Hydra

```yaml
# configs/config.yaml
hydra:
  callbacks:
    argus:
      _target_: argus.integrations.hydra.ArgusCallback
      project: my-paper
      experiment_type: forecast
```

`main.py` is unchanged. Set `ARGUS_URL` / `ARGUS_TOKEN` in the env and every
`python main.py …` (or `-m` for a sweep) emits one batch with N jobs.

The adapter handles single-run vs multirun automatically and stamps the
right Hydra job number as `job_id`. See [Hydra callback](../sdk/hydra-callback.md)
for full options (custom job ids, daemon toggles, Optuna study labels for
the Studies tab).

## Crash-resume

For long sweeps that may crash mid-flight, use `derive_batch_id` to
guarantee the relaunched run lands on the same Batch row:

```python
from argus import Reporter, derive_batch_id

batch_id = derive_batch_id("my-paper", "dam_forecast")  # uses git rev-parse HEAD
with Reporter(source_project="my-paper",
              experiment_type="forecast",
              n_total=120,
              batch_id=batch_id) as r:
    ...
```

Same checkout → same id → same Batch row on the backend. See
[Batch identity & resume](../sdk/resume.md).

## What gets sent

* **Heartbeat** every 5 minutes (keeps the batch in *running* state).
* **Stop-signal poll** every 10 seconds (reads `/api/batches/{id}/stop-requested`).
* **Resource snapshot** every 30 seconds (CPU / RSS / GPU util / GPU mem).
* **Job epoch** when you call `job.epoch(...)`.
* **Log line** when you call `job.log(...)` (off until you call it).

All events carry a UUID `event_id`; the backend dedupes by it, so retried
posts after a network hiccup are safe.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 Unauthorized | Token revoked, or you copied the prefix-only display |
| Batch stuck *running* | Heartbeat skipped; backend marks it *stalled* after 15 min (`ARGUS_STALL_TIMEOUT_MIN`) |
| `ARGUS_DISABLE=1` set | SDK no-ops; check the env in the runner |
| `ARGUS_URL` not picked up | Older code may have used `MONITOR_URL` — that name is no longer recognised |

For deeper SDK reference see [Reporter API](../sdk/reporter.md).
