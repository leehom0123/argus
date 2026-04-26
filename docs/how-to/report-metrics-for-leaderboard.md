# How to make your training show up on the leaderboard

Argus does not compute metrics. The leaderboard reads whatever your
training code puts into the `data.metrics` dict of the `job_done`
event. **The set of columns you see is the union of every key any
job has ever reported in that project.** That is intentional — Argus
is task-agnostic, so metric names are part of your training contract,
not Argus's schema.

## The minimum a job must emit

| Step | Event | When |
|---|---|---|
| 1 | `batch_start` | At the start of a benchmark or single run |
| 2 | `job_start`   | Per (model x dataset x seed) job |
| 3 | `job_epoch` x N | Per epoch — used for the loss curve, NOT the leaderboard |
| 4 | `job_done`    | At end of test() — **this is what populates the leaderboard** |
| 5 | `batch_done`  | When all jobs in the batch finish |

Only step 4 affects the leaderboard. Steps 1, 2, 3, 5 drive the
status badge, loss curve, and ETA.

## What goes in `job_done.data.metrics`

Anything you want. Argus stores the dict as-is in `Job.metrics` (a
JSON column), and the leaderboard endpoint reads each key out at
query time so it can become a column in the UI or the CSV export.
Add a new key, get a new column.

The platform never validates metric names — `MSE`, `accuracy`,
`bleu`, `dice_score`, `wall_clock_seconds`, anything goes. The
columns you see in the screenshots below are conventions in
forecasting and inference benchmarking, NOT Argus requirements.

## Conventions used by the time-series forecasting workflow

If you want to reuse the time-series ranking UI without writing a
new view, follow this naming so values land in the expected columns:

**Quality metrics**: `MSE`, `MAE`, `RMSE`, `R2`, `PCC`, `sMAPE`,
`MAPE`, `MASE`, `RAE`, `MSPE`

**Throughput / latency**: `Latency_P50`, `Latency_P95`, `Latency_P99`,
`Inference_Throughput`, `Inference_Time_Per_Sample`,
`Total_Inference_Time`, `Samples_Per_Second`

**Compute footprint**: `GPU_Memory`, `GPU_Memory_Peak`,
`GPU_Utilization`, `CPU_Memory`, `CPU_Utilization`,
`Total_Train_Time`, `Avg_Epoch_Time`, `Avg_Batch_Time`,
`Total_Batches`

**Model meta**: `Model_Params`, `Model_Size`, `seed`

These names are not magic. They are the keys our reference
forecasting trainer happens to write. Add your own and they show
up next to these in the UI.

## Walkthrough

A minimal `Reporter` usage that emits every step above is shipped
in `client/examples/leaderboard_full_demo.py`. Run it against any
Argus instance:

```bash
export ARGUS_URL=https://argus.example.com
export ARGUS_TOKEN=em_live_xxxxxxxxxxxxx
python client/examples/leaderboard_full_demo.py
```

Then refresh the project's leaderboard tab (project
`leaderboard-demo`) to see the demo job.

The example also accepts `--dry-run` so you can see the events that
*would* be emitted without contacting any server:

```bash
python client/examples/leaderboard_full_demo.py --dry-run
```

## Common mistakes

- Forgetting to emit `job_done`: the row shows up in the *jobs* list
  but never on the leaderboard. The leaderboard only ranks jobs in
  status `done`, which is set by the `job_done` event.
- Putting metrics in `job_epoch` instead of `job_done`: epoch metrics
  go into the loss curve, not the leaderboard. Final test metrics
  must arrive in `job_done`.
- Forgetting the unit. `Latency_P50` is conventionally seconds, not
  milliseconds; `GPU_Memory` is conventionally MB. The UI assumes
  these so its sparkline scales make sense — pick one unit per key
  and stick with it.

## Custom metrics and ranking

Once a key appears in any `job_done.metrics` dict, it becomes
available as a sort column. Click the column header on the
leaderboard to sort by it. There is no platform-side schema to
extend.

The default ranking metric on the leaderboard endpoint is `MSE`;
the matrix view lets you switch the active metric from the dropdown
in [Projects & batches](../user-guide/projects-batches.md). If your
project is not a forecasting project, just pick whatever metric you
do report as the sort column — the rows are the same either way;
only the order changes.
