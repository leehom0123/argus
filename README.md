# Argus

> Self-hostable ML experiment monitoring — batches, jobs, GPU/CPU resources,
> reruns, hyperopt sweeps. Real-time dashboard. Multi-user. One container.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-MkDocs%20Material-brightgreen.svg)](https://leehom0123.github.io/argus/)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.10-blue.svg)](#)
[![Node](https://img.shields.io/badge/node-%E2%89%A520-green.svg)](#)

[English](README.md) · [简体中文](README.zh-CN.md) · [Documentation](https://leehom0123.github.io/argus/)

---

## A familiar story

It's 23:00. You launched a 32-config Optuna sweep on the lab's
workstations and went to dinner. At 02:00 you wake up worried, SSH in,
and discover:

- two of the eight GPUs have sat at 0% for four hours — a dataloader stalled and the loop never crashed;
- one trial died at epoch 47 of 50 with `CUDA out of memory`, but the
  only trace is a 600 MB `stdout.log` you don't want to read;
- when you re-run the dead trial it lands as a *new* batch in your
  notes, fragmenting the sweep across two rows;
- your advisor pings: "which trial won — can I see the loss curve?";
- the PI on Slack: "is anyone using `gpu-03` right now?".

Argus is the dashboard the author built after losing several
nights to that exact story. It's an open-source experiment tracker for
teams running long ML training jobs across one or many machines. You
instrument your training script with a two-line `Reporter` SDK call,
and Argus answers the questions above without your having to SSH in.

## Pain → feature

| The pain at 02:00 | What Argus does |
|---|---|
| GPU has been at 0% for hours, training loop didn't notice | **Idle-job detector** — `resource_snapshot` events every 30 s; flagged when GPU util &lt; 5% for `ARGUS_IDLE_JOB_THRESHOLD_MIN` (default 10) min |
| Training script crashed but Argus still says *running* | **Stalled-batch detector** — flips status to *stalled* after `ARGUS_STALL_TIMEOUT_MIN` (default 15) min of silence |
| Resumed run becomes a new batch — sweep history fragments | **Crash-resume** — `derive_batch_id(project, experiment, git_sha)` returns a deterministic id; pass `Reporter(resume_from=…)` and the resumed run appends to the original Batch row |
| Wandering through 600 MB of stdout to find the failure | **Tail of `log_line` events** on the job detail page; full log streamed via SSE |
| "Which trial won?" across a 32-config sweep | **JobMatrix** — one global best (green border + trophy) and one global worst (red border + warning) on the primary metric across the whole `model × dataset` grid; CSV export for paper tables |
| "Show me the loss curves of trial #17" without a login | **Per-batch share links** — opaque slug, read-only, revocable; or set the project visibility to public |
| 200 lines of W&B / CSV glue per training script | Two `with` blocks: `with Reporter(...)` opens a *batch*, `with r.job(...)` opens a *job*, `job.epoch(...)` streams metrics; failures auto-emit `job_failed` |
| Optuna sweeps need their own dashboard | **Studies tab** — scatter, parallel-coordinates and parameter-importance from `optuna.{study_name, trial_number}` labels Sibyl's monitor callback stamps on `job_start` |
| "Re-run that failed trial, but I'm at home and the box is on campus" | **Rerun button** on a finished batch sends a `kind=rerun` command to the `argus-agent` daemon on the origin host; the agent calls `subprocess.Popen` with the recorded `env_snapshot.command` |
| "Stop this run, I changed the hyperparameter" | **Stop button** flips a flag your loop reads via `if job.stopped: break` (10 s polling); the agent escalates to `SIGTERM` if the SDK isn't running |
| Co-authors all want notifications, but each on their own terms | **Per-user prefs** (email-on-batch-done, email-on-job-failed, daily digest) **+ per-project recipient routing** — a project mails its event list, but each recipient's prefs win |
| "Where did this batch's command line go?" | `env_snapshot` (git SHA, command, cwd, hostname) recorded on `batch_start`; one-click *Copy command* in the UI |
| Hydra training, want monitoring without code changes | `argus.integrations.hydra.ArgusCallback` — wire under `hydra.callbacks` once, every `python main.py …` (and `-m` sweeps) emits a batch automatically |
| Lightning / Keras training, same story | `argus.integrations.{lightning,keras}.ArgusCallback` drop-in callbacks |
| "I want to deploy this on our airgapped cluster" | Single Docker image (FastAPI + Vue 3 + SQLite by default). No SaaS, no telemetry. Optional Postgres for multi-host. |

## Quickstart (60 seconds)

```bash
git clone https://github.com/leehom0123/argus.git
cd argus/deploy
cp .env.example .env

# Generate a JWT secret (>=32 bytes is enforced when ARGUS_ENV=prod)
python3 -c "import secrets; print('ARGUS_JWT_SECRET=' + secrets.token_urlsafe(48))" >> .env

docker compose up -d --build
# Open http://localhost:8000 — register the first account; it becomes admin.
```

Then, in your training script:

```python
from argus import Reporter

with Reporter("my-run",
              experiment_type="forecast",
              source_project="my-paper",
              n_total=1,
              monitor_url="http://localhost:8000",
              token="em_live_…") as r:                # mint at Settings → Tokens
    with r.job("run-1", model="patchtst", dataset="etth1") as job:
        for epoch in range(50):
            # JobContext.epoch defines four named optional floats —
            # train_loss, val_loss, lr, batch_time_ms — plus **extra:
            # any other keyword is stored on the event payload, so you
            # can stream as many per-epoch metrics as you compute.
            job.epoch(epoch,
                      train_loss=..., val_loss=..., lr=..., batch_time_ms=...,
                      val_mse=..., val_rmse=..., val_mae=...,
                      val_r2=..., val_pcc=...)
        # Final / headline metrics for the run — any dict[str, float],
        # surfaced on the JobMatrix and CSV export. Drop everything you
        # want plotted on the leaderboard here.
        job.metrics({
            "MSE":  ..., "RMSE": ...,
            "MAE":  ..., "R2":   ...,
            "PCC":  ...,
        })
```

Or set the env vars and skip the explicit args:

```bash
export ARGUS_URL=http://localhost:8000          # SDK reads ARGUS_URL (not ARGUS_BASE_URL)
export ARGUS_TOKEN=em_live_…
```

That's it — refresh the dashboard and your batch is live.

## Repository layout

```
argus/
├── backend/    FastAPI + async SQLAlchemy 2.0 + Alembic (Python ≥3.10)
├── frontend/   Vue 3 + TypeScript + Vite + Pinia + Ant Design Vue (4.2.6 pinned)
├── client/     argus-reporter SDK (PyPI: argus-reporter)
├── schemas/    Event contract (event_v1.json, schema v1.1)
├── deploy/     Docker compose, Dockerfile, nginx snippet, .env.example
└── docs/       MkDocs Material site (English + 简体中文)
```

## Feature highlights

| Area | What's there |
|---|---|
| **Tracking** | Batches → jobs → epochs hierarchy; idempotent ingest by `event_id`; JSONL spill replay on outage |
| **Live UI** | Single multiplexed SSE connection (`GET /api/sse`) per page; ECharts loss curves; GPU/CPU sparklines |
| **Auth** | Email + password (argon2id); GitHub OAuth (optional); JWT dual-key rotation |
| **Tokens** | `em_live_*` for SDK, `ag_live_*` for agents — both bound per user |
| **Executor** | Backend serves `/api/agents/*`. The agent daemon (`argus-agent`) ships in the Sibyl package; reruns use `subprocess.Popen`, stops use SIGTERM |
| **Studies** | Optuna multirun via `optuna.{study_name, trial_number}` labels stashed by Sibyl's monitor callback |
| **Notifications** | Per-user email prefs + per-project multi-recipient routing; SMTP delivery |
| **Runtime config** | GitHub OAuth, SMTP, retention, demo project, feature flags editable from UI without redeploy |
| **i18n** | Full English + Simplified Chinese UI |
| **Frameworks** | First-class PyTorch Lightning + Keras + Hydra callback adapters (`argus.integrations.{lightning,keras,hydra}`) |
| **Resume** | `derive_batch_id` keeps a relaunched experiment on the same Batch row |

## Documentation

| | |
|---|---|
| 🚀 [Getting started](docs/getting-started/installation.md) | Install, first run, send first event |
| 📖 [User guide](docs/user-guide/dashboard.md) | Dashboard, batches, jobs, sharing, notifications |
| 🐍 [SDK reference](docs/sdk/reporter.md) | `Reporter` API, framework adapters, event schema |
| 🛠 [Operations](docs/ops/docker.md) | Docker, configuration, admin settings, agent, database, retention |
| 🏗 [Architecture overview](docs/architecture-overview.md) | How the pieces fit together |
| 📝 [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) | |

## License

Apache-2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

If you use Argus in academic work, please cite via [`CITATION.cff`](CITATION.cff).
