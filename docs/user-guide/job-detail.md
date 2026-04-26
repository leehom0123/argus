# Job detail

The job detail page is the leaf view. It is a single scrollable layout. The route is
`/jobs/{batch_id}/{job_id}` in the SPA, served by `JobDetail.vue`.

## Layout

* **Telemetry strip** — status / elapsed / GPU util / GPU mem peak / latest loss
* **Loss curve** — ECharts line plot, fed by the SSE stream
* **Log tail** — the most recent `log_line` rows, if any
* **Action bar** — Stop · Rerun · Share · Copy command

## Telemetry strip

Compact tiles populated from the latest events:

| Tile | Source |
|---|---|
| Status | latest event |
| Elapsed | `job_start` to last activity |
| GPU util | latest `resource_snapshot` (mean across visible GPUs) |
| GPU mem peak | rolling max from snapshots |
| Latest loss | last `job_epoch` train/val loss |

The dashboard idle detector flags this job when GPU util has stayed below
5% for `ARGUS_IDLE_JOB_THRESHOLD_MIN` minutes (default 10) — typical
"dataloader stalled, GPU has been idle for an hour" footgun.

## Loss curve

ECharts line plot streamed via SSE. By default `train_loss` and `val_loss`
are drawn (any `**extra` numeric metric on `job.epoch` becomes another
toggleable series).

## Log tail

`GET /api/batches/{batch_id}/log-lines` powers this panel. Logs are not
stored forever — the retention sweeper purges `log_line` events older than
`ARGUS_RETENTION_LOG_LINE_DAYS` days (default 14). Summary rows survive
indefinitely.

## Action bar

| Button | Endpoint | Notes |
|---|---|---|
| **Stop** | `POST /api/batches/{batch_id}/stop` | Sets a flag; SDK reads on next poll (10 s default) |
| **Rerun** | `POST /api/batches/{batch_id}/rerun` | Sends a `kind=rerun` to the origin host's `argus-agent` (shipped in Sibyl). Disabled if no agent online. |
| **Share** | shares API | Mints a public read-only link (covers the whole batch) |
| **Copy command** | client-side | Copies the recorded `env_snapshot.command` |

## Env snapshot

An **Environment** panel below the action bar shows what the SDK captured
on `batch_start`:

* Git commit (short SHA)
* Command line, working directory, hostname

This is what gets re-executed on a rerun.

## Failed jobs

If the run ended in `job_failed`, the failure reason is shown above the log
tail.

## See also

* [Argus Agent](../ops/argus-agent.md) — what powers Rerun.
* [Sharing](sharing.md) — link semantics.
