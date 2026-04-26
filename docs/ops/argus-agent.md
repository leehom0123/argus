# Argus Agent

The **Argus Executor** is split in two halves. Argus owns the server half
(`/api/agents/*` routes); the host-side daemon — `argus-agent` — is shipped
by the **Sibyl** sister package as `sibyl.executor.agent`. The two were
co-designed so they upgrade together and share a token vocabulary
(`em_live_…` for SDK / parent, `ag_live_…` for agents).

If you do not run Sibyl on your training hosts, the **Rerun** button in
Argus stays disabled — that is the only feature affected by not having an
agent. Everything else (event ingest, dashboards, sharing, notifications)
works without it.

## What the agent does

* On register, mints an `ag_live_…` token bound to the user whose
  `em_live_*` token authorised the registration. Token cached on the host
  in `~/.argus-agent/token.json`, mode `0600`.
* Heartbeats periodically — host turns up as **online** in the UI.
* Polls `/api/agents/{id}/jobs` for new commands.
* For `kind=rerun`: spawns `subprocess.Popen` for the recorded
  `env_snapshot.command` from the batch's origin host, captures the PID,
  and acks via `/api/agents/{id}/jobs/{cmd_id}/ack`.
* For `kind=stop`: sends `SIGTERM` to the recorded PID's process group;
  escalates to `SIGKILL` after a grace period if the process refuses.

## Server endpoints

The agent talks to four endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/agents/register` | Register a host; mint `ag_live_…` |
| `GET` | `/api/agents/{agent_id}/jobs` | Poll for queued commands |
| `POST` | `/api/agents/{agent_id}/jobs/{cmd_id}/ack` | Acknowledge a command's PID |
| `POST` | `/api/agents/{agent_id}/heartbeat` | Keep the online badge green (204 No Content) |

## Install & first-time register

The agent is part of the Sibyl distribution. From the training host:

```bash
pip install sibyl-ml          # ships argus-agent as a console script

export ARGUS_URL=https://argus.example.com
export ARGUS_TOKEN=em_live_…  # SDK token (parent), used to mint the agent token

argus-agent --register        # one-shot: hits /api/agents/register, caches ag_live_…
argus-agent                   # foreground run; Ctrl-C to stop
```

`--register` is one-shot; subsequent runs of `argus-agent` (no args) read
the cached token. The exact command-line surface is owned by the Sibyl
package — see Sibyl's executor agent docs for `--reregister`, debug flags,
and systemd templates.

## Threat model

The agent is privileged: it can run arbitrary shell commands as its OS user.
Treat each `ag_live_*` token as if it controls the host's user account.

* **Token scope**: tied to the `em_live_*` parent's owner. Argus rejects
  rerun requests whose requester is not the batch's owner *or* a project
  recipient.
* **Token storage**: cached file is mode `0600`. Don't commit it.
* **Command source**: only `env_snapshot.command` from the recorded batch is
  executed — there is no general "run arbitrary string" endpoint.
* **Network direction**: outbound only (the agent is the client; nothing
  needs to listen). HTTPS is enforced when `ARGUS_URL` is `https://`.

## Multiple agents per host

A user can run several agents under different OS users on the same machine;
tokens are independent. The exact label / multi-instance flags are part of
the Sibyl agent's CLI — refer to its docs.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Rerun button greyed out | No agent online for the originating host. Run the agent on that host. |
| `401` on register | `ARGUS_TOKEN` is missing / revoked |
| Rerun launches but training fails | The recorded `command` references files that no longer exist |
| Stop doesn't take effect | Training script ignores `SIGTERM`; use `job.stopped` in your loop or pin the agent's grace period |

## See also

* [Architecture overview](../architecture-overview.md) — where the agent fits.
* [Job detail](../user-guide/job-detail.md) — Rerun / Stop in the UI.
* `sibyl/sibyl/executor/agent.py` — the agent source (in the Sibyl repo).
