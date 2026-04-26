# #103 Argus Executor Architecture — Rerun Runner + Pause/Resume Design

> Status: **DRAFT for v0.1.4 / v0.1.5** • Owner: architect agent • Date: 2026-04-26
> Scope: turn the four lifecycle primitives (start / **stop** / **pause** / **resume** / **rerun**) into a unified, idempotent service with a working runner. Today only `stop` and a "fake" `rerun` (DB stub) exist.

---

## 1. Problem Statement

`POST /api/batches/{id}/rerun` clones the batch row, writes a `rerun_requested` event, and returns 201 — but **nothing on the experiment host actually launches `main.py` again**. The reporter side has no `rerun-info` poll baked in, so the new batch sits in `status='requested'` forever until a human SSH's in and runs the command. The user calls this **"fake rerun"**.

Pause/resume don't exist at all: zero endpoints, zero reporter polling, zero UI.

`stop` works: API flips status, reporter polls `/stop-requested`, monitor callback exits at the next epoch boundary.

**Goal of #103**: ship a unified Executor service so all four primitives are real, idempotent, and observable, and pick the smallest viable slice for v0.1.4.

---

## 2. Rerun Runner — the central decision

### Trade-off matrix

| Option | Push from Argus | SSH creds storage | Symmetry with `stop` | Operator burden | Security blast radius |
|---|---|---|---|---|---|
| **A. SSH from Argus** | Yes (paramiko) | Per-host credentials in Argus DB | Asymmetric (stop is pull, rerun is push) | Operator must register SSH key per host | High — Argus becomes an SSH client to every training host |
| **B. Agent daemon on host** | No (host pulls) | Agent token only | Symmetric — agent polls just like reporter does for stop | Operator runs one extra daemon per host (`systemd --user`) | Low — only outbound from host to Argus |
| **C. Webhook** | Yes (HTTPS) | None on Argus | Asymmetric | Host must expose inbound HTTPS — usually impossible behind NAT/cluster | Medium |

### Recommendation: **Option B — agent-based pull**

Justification:

1. **Architectural symmetry**. `stop` already works by polling Argus from the host (`GET /api/batches/{id}/stop-requested`). The reporter callback (`client/argus/context.py:_stop_poll_loop`) is the existing template; the agent is the same loop applied at the **host** level instead of the **process** level. We extend a working pattern rather than introducing a new push direction.
2. **No inbound from Argus → hosts**. Most training nodes (lab boxes behind NAT, GPU servers reachable only via jump hosts) sit behind a perimeter. SSH from Argus would force the operator to maintain reverse tunnels or open inbound SSH for an Argus public IP. The agent only needs outbound HTTPS to Argus, which is already the reporter's network footprint.
3. **No credential surface on Argus**. With Option A every host's SSH private key (or password) lives in the Argus DB. One Argus compromise → root on every training cluster. With Option B a stolen agent token can only enqueue training runs on that one host, and tokens are already revocable.
4. **Reuses reporter packaging**. `argus-reporter` is already pip-installable. The agent ships as `argus-reporter agent` — same wheel, new console-script entrypoint, same auth pipeline.
5. **Fits Sibyl's existing process model**. Each host already has one persistent reporter Python process per batch; an "agent" is just a longer-lived sibling that listens for *new* batch creation rather than reporting on a current one.

### Agent contract (sketch)

```
host                                 Argus
 │                                    │
 │  GET /api/agents/me/poll  (every 5s, with host_id + agent_token)
 │  ◄──── { rerun: [{batch_id, command, cwd, env, host_id}],
 │          pause: [batch_id, ...],
 │          resume: [{batch_id, checkpoint_path, command, ...}] }
 │
 │  spawn subprocess.Popen(command, cwd=cwd, env=env)
 │  POST /api/batches/{id}/ack-launched   (reporter takes over from here)
```

The agent never owns metrics or events; it only spawns/kills processes. Once a process is up, the in-process reporter takes over the existing per-batch event stream.

**Hosts that can't run an agent** (one-off bare-metal, ad-hoc SSH boxes) keep the manual rerun flow: the new batch sits in `status='requested'` and the human runs `argus-reporter pickup <batch_id>` by hand. This is the documented escape hatch, not a regression.

---

## 3. Pause/Resume Mechanics

Sibyl's trainer is a synchronous PyTorch loop. The natural pause point is the **epoch boundary**, where checkpoints are already written (`checkpoint_last.pt` carries model + optimizer + scheduler + RNG states; see CLAUDE.md "Deterministic resume" + "FEDformer index persistence").

### Does pause/resume reduce to `stop + rerun --resume`? **Yes — with one caveat.**

- **Pause = stop with intent**. The Sibyl monitor already exits cleanly at epoch boundary on `stop_requested`. The only difference between stop and pause is **operator intent**: `stop` means "throw it away, status=cancelled"; `pause` means "I want to come back, status=paused, keep the run dir". Both produce the same on-disk artefact (`checkpoint_last.pt`).
- **Resume = rerun where the runner injects `resume=true` + `resume_path=<paused run's checkpoint_last.pt>`**. The receiving Sibyl already supports this flag (CLAUDE.md "Resume after Ctrl+C").

So pause/resume is **sugar over stop+rerun** with three thin additions:

1. New `pause` event type and `Batch.status='pausing' | 'paused'` so we can semantically distinguish "user wants this back" from "user threw it away".
2. Resume endpoint must point the rerun at the *same batch row* (not a clone), so the leaderboard/timeline don't fragment. Concretely: `POST /api/batches/{id}/resume` mints `resume_requested` event but **does not create a new Batch row** — the agent picks it up and re-launches with `resume_path` pointing at this batch's own `checkpoint_last.pt`. (This is the caveat: rerun creates a new row, resume does not.)
3. `stop` semantics are unchanged. A user who clicks "Pause" on a running batch and then changes their mind to "Stop" can — `stop` overrides `pausing`.

### State machine

```
                ┌─────────────────────────── stop ─────────┐
                │                                          ▼
            ┌────────┐  pause  ┌──────────┐  exit-clean  ┌─────────┐
            │running │ ──────► │ pausing  │ ───────────► │ paused  │
            └───┬────┘         └────┬─────┘              └────┬────┘
                │                   │                         │
                │ stop              │ stop                    │ resume
                ▼                   ▼                         ▼
            ┌────────┐         ┌──────────┐              ┌──────────┐
            │stopping│ ──────► │cancelled │              │resuming  │
            └────────┘         └──────────┘              └────┬─────┘
                                                              │ agent launches
                                                              ▼
                                                          ┌─────────┐
                                                          │ running │
                                                          └─────────┘
```

### `Batch.status` enum extension

Existing: `running | stopping | stalled | done | failed | cancelled | requested | pending`.
Add: `pausing | paused | resuming`.

**Consumers to audit**:
- `frontend/src/composables/useStatusColor.ts` — bucket new states (`pausing → running`-bucket yellow; `paused → done`-bucket grey; `resuming → running`-bucket).
- `StatusTag.vue` — i18n keys + colour token.
- `Dashboard.vue` running-tile counter — `paused` counts as not-running.
- `retention.py` retention sweep — exclude `paused` from auto-archive (otherwise we delete a checkpoint a user is about to resume!).
- `services/health.py` `is_stalled` watchdog — paused batches are NOT stalled (they're intentionally idle).
- `services/eta.py` ETA calculator — pause should freeze ETA.

---

## 4. Job-Retry Policy (separate from rerun)

**Punt to v0.1.5.** The user's question was about batch-level lifecycle ("停止 / 继续 / 暂停 / 重跑"); per-job auto-retry is a different feature with its own design (need failure classification — OOM vs cuda-error vs metric-NaN-divergence — to avoid retry storms). The existing `RerunModal` already handles "rerun a failed batch with overrides" which covers the manual case. Track job-retry as #104 in v0.1.5.

---

## 5. Argus Executor Service Architecture

New module: `backend/backend/services/executor.py`. All four primitives go through one class:

```python
class ExecutorService:
    async def request_start(batch_id, command, host, env_snapshot) -> Event
    async def request_stop(batch_id, requested_by) -> Event
    async def request_pause(batch_id, requested_by) -> Event
    async def request_resume(batch_id, requested_by) -> Event   # in-place, no clone
    async def request_rerun(batch_id, overrides, requested_by) -> str  # returns NEW batch_id
```

Each method: (1) idempotency guard via current `Batch.status`, (2) status flip in same txn as event row, (3) emit typed SSE event so connected dashboards refresh without polling.

**Idempotency table** (re-clicks become no-ops):

| Action | Allowed source states | Re-click on target state |
|---|---|---|
| stop | running, stalled, pausing, paused | already-stopping/cancelled → 200 no-op |
| pause | running | pausing/paused → 200 no-op |
| resume | paused | resuming/running → 200 no-op |
| rerun | done, failed, cancelled | always allowed (mints new row) |

API routes in `backend/backend/api/batches.py` shrink to thin wrappers calling the service. Audit logging stays at the route layer (existing pattern).

**SSE event types** added to `events_stream.py`:
`batch.stop_requested`, `batch.pause_requested`, `batch.resume_requested`, `batch.rerun_requested` — each carrying `{batch_id, requested_by, requested_at}`. Frontend `BatchDetail.vue` already has an SSE subscription; one switch-case adds the four handlers.

---

## 6. Sibyl-Side Reporter Changes

### Reporter (`client/argus/context.py`)

Already has `_stop_poll_loop`. Add `_pause_poll_loop` calling `GET /api/batches/{id}/pause-requested`. On signal: set `self._paused_remote.set()`. The Sibyl monitor callback (`sibyl/callbacks/monitor.py`) reads `is_pause_requested()` at epoch boundary, calls `trainer.save_checkpoint('checkpoint_last.pt')`, then `sys.exit(0)` with a sentinel exit code (`exit 50` = paused-clean).

Resume needs no client-side change. The agent (Section 2) starts the new process with `python main.py ... resume=true resume_path=<dir>/checkpoints/checkpoint_last.pt`, which is already implemented in Sibyl's `main.py` per CLAUDE.md.

### Agent (new: `client/argus/agent.py`)

Console script `argus-agent`. Runs as `systemd --user` on each host. Polls `/api/agents/me/poll` every 5 s, spawns `subprocess.Popen` for each work item, hands subprocess PID back via `POST /api/agents/me/ack`. Tracks alive PIDs in a local SQLite for crash recovery.

---

## 7. Frontend UX

### BatchDetail.vue action row (current location: line 810)

```
┌─────────────────────────────────────────────────────────┐
│ status: ● running                                       │
│                                                         │
│ [⏸ Pause]  [⏹ Stop]                                     │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ status: ⏸ paused                                        │
│                                                         │
│ [▶ Resume]  [⏹ Stop]  [⟳ Rerun]                         │
└─────────────────────────────────────────────────────────┘
```

Pause sits left of Stop (less destructive → leftward). Rerun moves into the action row when terminal-state, replacing the existing top-right Rerun button.

### Status badge colour cycle

| State | Colour bucket | Animation |
|---|---|---|
| running | green | pulsing dot |
| pausing | yellow | spinner |
| paused | grey-blue | static |
| resuming | yellow | spinner |
| stopping | red-orange | spinner |
| cancelled | red | static |

### Confirmation modal copy

- **Pause**: "Pause this batch? Training will stop at the end of the current epoch and a checkpoint will be saved. You can resume later from the same point." • OK: "Pause"
- **Resume**: "Resume training from epoch N (checkpoint saved Apr 26 14:32)?" • OK: "Resume"
- **Stop while paused**: "This batch is paused. Stopping will discard the checkpoint. Continue?" — high-friction copy because we're throwing away work.

---

## 8. Migration & Backward Compatibility

### Old reporters (sibyl-ml < 0.1.5)

They never poll `/pause-requested`. Argus must **degrade gracefully**: when a pause is requested on a batch whose reporter version (recorded in `env_snapshot_json.reporter_version` since v0.1.3) is < 0.1.5, the API returns 409 with `detail="reporter_too_old"`. Frontend shows toast "Pause requires reporter ≥ 0.1.5; please upgrade or use Stop". Hard-error is correct here — silently dropping the pause would be worse UX than a clear failure.

### Stale `env_snapshot_json` on rerun

A batch from 6 months ago may have `cwd=/mnt/data/CC/old-path` that no longer exists, or `conda env=ts` since renamed to `ts-2026`. The agent runs the command and `subprocess.Popen` fails immediately — exit code captured, batch flips to `failed`, rerun event annotated with `failure_reason='stale_env_path'`. We do NOT try to "fix" stale paths automatically; the user re-fills via the existing RerunModal overrides field. Document this in the runner FAQ.

---

## 9. Testing Plan

### Unit (backend, ~400 LOC)
- `test_executor_request_stop_idempotent` — re-clicking stop is no-op.
- `test_executor_request_pause_from_running` — flips status, writes event.
- `test_executor_request_pause_from_done_returns_409` — bad source state.
- `test_executor_request_resume_in_place` — does NOT mint new Batch row.
- `test_executor_request_rerun_mints_new_batch` — clones source row, sets `source_batch_id`.
- `test_executor_emits_sse_event_on_each_action` — typed event leak check.
- `test_status_enum_includes_pause_states` — schema migration check.

### Unit (client, ~200 LOC)
- `test_pause_poll_loop_sets_event` — reporter receives signal.
- `test_agent_poll_returns_rerun_work_items` — agent dequeue.
- `test_agent_spawn_subprocess_records_pid`.

### Integration (~300 LOC, slow tests)
- `test_e2e_stop_resume_round_trip` — fake reporter loop, request pause, save checkpoint, request resume, verify epoch index advances.
- `test_e2e_rerun_via_agent_spawns_process` — agent dequeue → subprocess → reporter → done.
- `test_e2e_old_reporter_pause_returns_409` — reporter_version compatibility gate.
- `test_e2e_paused_batch_excluded_from_retention_sweep` — retention safety.

### Frontend (~150 LOC)
- `BatchDetail.spec.ts` — pause button visibility per status.
- `useStatusColor.spec.ts` — new states bucket correctly.
- `RerunModal.spec.ts` — resume vs rerun branching (modal already exists; reuse).

---

## 10. Sizing & Estimate

| Piece | BE LOC | FE LOC | Test LOC | Days (1 BE + 1 FE + 1 QA) |
|---|---|---|---|---|
| `executor.py` service + 4 endpoints | 450 | — | 200 | 3 |
| Status enum migration + consumer audit | 80 | 120 | 80 | 1.5 |
| Reporter pause poll loop + monitor hook | 100 (client) | — | 80 | 1.5 |
| **Agent daemon** (new) | 600 (client) | — | 250 | 4 |
| Frontend pause/resume buttons + status cycle | — | 250 | 150 | 2 |
| SSE event types + Dashboard wiring | 60 | 80 | 60 | 1 |
| Migration & compat (409 gate, version check) | 80 | 50 | 60 | 1 |
| Docs (runner FAQ, agent install guide) | — | — | — | 1 |
| **Total** | **~1370 BE** | **~500 FE** | **~880 tests** | **~15 dev-days** |

Calendar time at 1+1+1: **~3 weeks** including review cycles and QA reruns.

---

## 11. Recommended Version Split

### v0.1.4 — Minimum viable "working rerun"
**Goal**: drop the "fake rerun" criticism. Ship rerun for real. Defer pause/resume.

- Executor service skeleton (only `request_rerun` and `request_stop` flow through it). [3 d]
- **Agent daemon** — minimum to spawn rerun subprocesses. No pause/resume work items yet. [4 d]
- Frontend: existing Rerun button now actually works end-to-end (status `requested → running` within seconds). [1 d]
- Docs: agent install guide. [0.5 d]
- **Total: ~8.5 dev-days, 2 weeks calendar.**

This makes the user's original complaint go away and gives us the agent infrastructure pause/resume needs.

### v0.1.5 — Pause / Resume
- Status enum migration + consumer audit. [1.5 d]
- Pause/resume endpoints + executor methods. [1.5 d]
- Reporter pause poll + Sibyl monitor hook. [1.5 d]
- Agent handles pause/resume work items. [1 d]
- Frontend pause/resume buttons + status cycle. [2 d]
- E2E round-trip tests. [1 d]
- **Total: ~8.5 dev-days, 2 weeks calendar.**

### v0.1.6 — Job-retry policy (#104)
- Per-batch `auto_retry_count` config.
- Failure classification (OOM / cuda-error / NaN / unknown).
- Retry-with-backoff at job level inside the agent.

### Why this split

The agent is the highest-risk piece (new daemon, new threat model, new install story). Landing it in v0.1.4 with only rerun lets us harden the agent on a single primitive before piling pause/resume on top. Pause/resume stacked on a working agent is an additive change, low blast radius.

---

## 12. Open Questions

1. **Agent install distribution**: ship `argus-agent` as a separate wheel or as a console-script in `argus-reporter`? Recommend latter (one less package to publish).
2. **Token model for agent**: reuse the per-host reporter token, or mint a new "host-level" token type with elevated `spawn` scope? Recommend new scope (`agent:spawn`) so a leaked reporter token can't launch arbitrary commands.
3. **What command does the agent actually run for resume?** Today Sibyl resume is `python main.py ... resume=true resume_path=...`. We need this exact string reconstructable from `env_snapshot_json`. Audit whether `command` field is preserved verbatim across rerun chains.
4. **Concurrent agents on the same host**: do we allow it? Recommend single-instance lock via PID file in `~/.argus/agent.lock`.
