# Projects and batches

Argus organises events in a three-level hierarchy:

```
Project ─┬── Batch ─┬── Job ─┬── Epoch metric
         │          │        ├── Resource snapshot
         │          │        └── Log line
         │          └── Batch metadata (env_snapshot, owner, command)
         └── Project-level settings (notification recipients, share links)
```

A **project** is what the SDK passes as `source_project=`. A **batch** is
what one `with Reporter(...)` opens. **Jobs** are runs inside a batch
(several for a sweep, one for a single training).

## Projects

### Listing & creating

Sidebar → **Projects**. Members see projects they belong to; admins see
everything. **New project** asks for a slug (immutable) and display name.

### Project detail

Tabs:

| Tab | What you find |
|---|---|
| Overview | Recent batches, summary counters, owners |
| Batches | Filterable batch list |
| Settings | Display name, notification recipients, share links, members |

### Visibility

A project is **private** (members only) or **public** (read-only to anyone,
including unauthenticated visitors). Members can be added by the project
owner or any admin.

### Notification recipients

Each project routes its email notifications to a list of recipients, not
just the creator (#116). Edit in *Settings → Recipients*. See
[Notifications](notifications.md) for triggers.

## Batches

### Listing

`GET /api/batches` is the underlying endpoint. The **Batches** page filters
by project, status, host, owner, and tag. URLs are deep-linkable.

### Batch detail

The header shows status, elapsed time, ETA (when computable), owner, host,
and the recorded command. The body is dominated by the **JobMatrix** (see
[Job matrix](job-matrix.md)) for sweeps, or a single-job summary card.

### Actions on a batch

| Action | Endpoint | Effect |
|---|---|---|
| Star | (front-end pin store) | Pinned to your starred list |
| Stop | `POST /api/batches/{id}/stop` | Sets a stop flag; SDK exits cleanly within ~10 s |
| Rerun | `POST /api/batches/{id}/rerun` | Asks `argus-agent` on the origin host to relaunch the recorded command |
| Share | (`shares` API) | Mints a read-only public link |
| Delete | `DELETE /api/batches/{id}` | Soft delete; sweeper purges per retention policy |
| Bulk delete | `POST /api/batches/bulk-delete` | Multi-select |
| Export CSV | `GET /api/batches/{id}/export.csv` | Per-job final metrics |

**Stop** is always available to the owner. **Rerun** requires an online
agent on the origin host (the agent is shipped by the Sibyl package — see
[Argus Agent](../ops/argus-agent.md)).

### Health & ETA

`GET /api/batches/{id}/health` flags a *stalled* batch when its newest event
is older than `ARGUS_STALL_TIMEOUT_MIN` minutes (default 15). The UI
surfaces this as an amber dot.

`GET /api/batches/{id}/eta` and `/jobs/eta-all` compute ETAs from completed
epoch durations. The UI hides the value until enough data points have been
collected to keep the estimate stable.

## See also

* [Job detail](job-detail.md)
* [Sharing](sharing.md)
