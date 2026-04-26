# Jobs list

The global **Jobs** page is a flat list across every job your account can see, with a filter bar — useful for cross-batch triage.

## Endpoint

The page is backed by `GET /api/jobs` — see
`backend/backend/api/jobs.py:233`. The response carries a global, paginated
list of jobs visible to the caller.

## Filters

The filter bar accepts:

| Filter | Notes |
|---|---|
| Status | running, done, failed, stalled, pending |
| Project | autocomplete by slug or name |
| Host | training host that emitted events |
| Batch | narrow to a single batch |
| Tags | matches labels on `job_start` |
| Since | last N hours / days |

URL parameters are bookmarkable.

## Columns

| Column | What |
|---|---|
| Status | Coloured pill (5-bucket palette) |
| Job ID | Click → job detail (`/api/jobs/{batch_id}/{job_id}`) |
| Batch | Parent batch (click → batch detail) |
| Project | Slug |
| Host | Hostname |
| Started | Relative time |
| Elapsed | Live for running, total for finished |

## RBAC

Non-admins see only jobs from projects they are a member of. Admins see
everything. Public projects' jobs are visible to logged-out visitors only
through share links (the global `/api/jobs` endpoint requires auth).

## Bulk delete

Multi-select supports bulk soft-delete via
`POST /api/jobs/bulk-delete` (`backend/backend/api/jobs.py:674`). The
request body is a list of `(batch_id, job_id)` pairs; the response reports
deleted count and per-row skip reasons. Soft-delete respects retention
sweeping.

## See also

* [Job matrix](job-matrix.md) — sweep view inside a batch.
* [Job detail](job-detail.md) — single run.
