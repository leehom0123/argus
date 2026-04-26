# First run

This walkthrough takes you from a freshly-started container to a live batch
on the dashboard, in under five minutes.

## 1. Register the first user

Open <http://localhost:8000> and click **Register**.

* The first registered account becomes **admin** — pick the address you'll
  log in with.
* If SMTP is not configured, the verify-email link is printed to the backend
  logs (`docker compose logs argus`).

After verifying you land on the **Dashboard**.

## 2. Create a project

A *project* is the namespace passed as `source_project=` from the SDK. From
the sidebar click **Projects → New project** and fill:

| Field | Example |
|---|---|
| Slug | `paper-2026` |
| Name | "Paper experiments 2026" |
| Visibility | Private (members only) / Public (read-only to anyone) |

The slug is what your training script passes. It is immutable once batches
exist under it.

## 3. Mint an SDK token

From **Settings → Tokens** click **Create token**.

* Tokens are prefixed `em_live_` and shown **once** — copy them now.
* They are bound to your user; deleting a user revokes their tokens.
* You can mint many; revoking one never affects others.

Tokens are stored hashed; the server verifies in constant time.

## 4. Push the demo batch

In a Python environment with `argus-reporter` installed
(`pip install argus-reporter`):

```python
import time, random
from argus import Reporter

with Reporter("warmup",
              experiment_type="forecast",
              source_project="paper-2026",
              n_total=1,
              monitor_url="http://localhost:8000",
              token="em_live_…") as r:
    with r.job("warmup-1", model="tiny", dataset="demo") as job:
        for epoch in range(5):
            time.sleep(1)
            job.epoch(epoch,
                      train_loss=1.0 / (epoch + 1) + random.random() * 0.1,
                      val_loss=1.2 / (epoch + 1) + random.random() * 0.1)
        job.metrics({"final_val_loss": 0.25})
```

(Or set `ARGUS_URL` / `ARGUS_TOKEN` and drop the `monitor_url` / `token` args.)

Refresh the dashboard. The batch appears under **Running**, then transitions
to **Done** with a green status pill when the script exits.

## 5. Open the batch

Click into the batch. You will see:

* A **status header** with elapsed time, ETA, owner, host.
* A **JobMatrix** — best-in-column metric values are highlighted; worst-in-column too.
* A **loss chart** that streams in via SSE.
* A **log tail** with stdout/stderr lines (only present if you called `job.log(...)`).

## 6. (Optional) Try a stop and rerun

While a batch is running, the **Stop** button on the batch page sets a flag
the SDK polls every 10 s. Inside your loop, check `job.stopped` and break
cleanly:

```python
for epoch in range(num_epochs):
    if job.stopped:
        break
    ...
```

To use **Rerun**, you need an `argus-agent` daemon registered for the host
that originally ran the batch. The agent is shipped by the **Sibyl**
package (`pip install sibyl-ml`) — see [Argus Agent](../ops/argus-agent.md)
for the full setup. Without an agent registered, the rerun button is
disabled.

## What's next

* [Connect a training job](connect-training.md) — Lightning, Keras.
* [User guide → Dashboard](../user-guide/dashboard.md) — every panel, explained.
* [Operations → Admin settings](../ops/admin-settings.md) — SMTP, GitHub OAuth, retention.
