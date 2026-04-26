# Dashboard

The dashboard is the home page after sign-in. It is read-only and streams
updates over the page's single multiplexed Server-Sent Events connection
(`GET /api/sse`) — there is nothing to refresh manually.

## Status palette

Argus uses one unified 5-bucket palette across cards, badges, and the
matrix view. Hex codes are pinned in `frontend/src/utils/status.ts`:

| Bucket | Hex | Ant Design tag |
|---|---|---|
| running | `#52c41a` (green) | green |
| done | `#d9d9d9` (grey) | default |
| failed | `#ff4d4f` (red) | red |
| stalled | `#faad14` (amber) | warning |
| pending | `#1677ff` (blue) | blue |

running and done.

## Panels

### Running batches

Tile cards for every batch in *running* state, sorted by start time
descending. Each card shows project, owner, host, and elapsed time. A green
"running" pill marks active batches; an amber dot marks stalled batches.

A batch is moved to *stalled* when no new event arrives for
`ARGUS_STALL_TIMEOUT_MIN` minutes (default 15). The detector runs every
`ARGUS_STALL_CHECK_INTERVAL_S` seconds (default 120).

### Recent failures

The last failed jobs across visible projects, with click-through to the job
detail page.

### Active hosts

Per-host strips. Tooltip shows CPU / GPU util / memory snapshots. Click a
host to open its detail page.

### Counters

Top strip with totals. Click any counter for a filtered drill-down.

## Visibility & RBAC

Non-admin users see only batches in projects they are a member of. Admins
see everything. Public projects (visibility=`public` on the project) are
visible to anyone, including unauthenticated visitors via share links and
the public-project routes.

## Streaming model

The dashboard subscribes to `GET /api/sse` once with a list of channels;
the server demultiplexes and the client routes each channel payload to the
right Pinia store.

If the connection drops (network, server restart, suspend-resume), the
client backs off and reconnects with `Last-Event-ID` so missed messages are
replayed.

## See also

* [Projects & batches](projects-batches.md)
* [Jobs list](jobs-list.md)
