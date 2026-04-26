# Argus

> Self-hostable ML experiment monitoring — batches, jobs, GPU/CPU resources,
> reruns, hyperopt sweeps. Real-time dashboard. Multi-user. One container.

Argus is an open-source experiment tracker for teams running long ML training
jobs across one or many machines. You instrument your training script with a
two-line `Reporter` SDK call, and Argus shows you batches, jobs, loss curves,
and live GPU/CPU telemetry in a Vue 3 dashboard.


## Find what you need

<div class="grid cards" markdown>

- :rocket: __[Getting started](getting-started/installation.md)__

    Install Argus with Docker, run for the first time, send your first event.

- :books: __[User guide](user-guide/dashboard.md)__

    Dashboard, batches, jobs, the matrix view, sharing, notifications, settings.

- :snake: __[SDK reference](sdk/reporter.md)__

    The `Reporter` API, Hydra/Lightning/Keras callbacks, the event schema.

- :wrench: __[Operations](ops/docker.md)__

    Docker, configuration, runtime admin settings, the agent, database, retention.

- :building_construction: __[Architecture](architecture-overview.md)__

    How the SDK, backend, frontend, and agent fit together.

- :handshake: __[Contributing](contributing.md)__

    Repo layout, dev loops, style, how to add a route / migration / integration.

</div>

## Snapshot

| | |
|---|---|
| **Backend** | FastAPI · async SQLAlchemy 2.0 · Alembic · Python ≥3.10 |
| **Frontend** | Vue 3 · TypeScript · Vite · Pinia · Ant Design Vue · ECharts |
| **SDK** | `argus-reporter` on PyPI; Lightning + Keras callbacks |
| **Database** | SQLite (default, WAL mode) or PostgreSQL via async driver |
| **Auth** | Email + password (argon2id), GitHub OAuth, JWT dual-key rotation |
| **Realtime** | One multiplexed Server-Sent Events connection per page |
| **Deployment** | Single Docker image, optional nginx reverse proxy |
| **License** | Apache-2.0 |

## A 60-second feel

```python
from argus import Reporter

with Reporter("my-run",
              experiment_type="forecast",
              source_project="my-paper",
              n_total=1,
              monitor_url="http://localhost:8000",
              token="em_live_…") as r:
    with r.job("run-1", model="patchtst", dataset="etth1") as job:
        for epoch in range(50):
            job.epoch(epoch,
                      train_loss=..., val_loss=..., lr=..., batch_time_ms=...,
                      val_mse=..., val_rmse=..., val_mae=...,
                      val_r2=..., val_pcc=...)
        job.metrics({
            "MSE": ..., "RMSE": ...,
            "MAE": ..., "R2":   ...,
            "PCC": ...,
        })
```

That's the whole integration. Heartbeats, GPU snapshots, stop-signal polling,
and idempotent retry-with-spill are all handled by the SDK.

## Where to next

If you have nothing running yet → [Installation](getting-started/installation.md).
If the server is up and you want to push events → [Connect a training job](getting-started/connect-training.md).
If you are deploying in production → [Operations](ops/docker.md) and [Admin settings](ops/admin-settings.md).
