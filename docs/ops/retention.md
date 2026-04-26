# Backups and retention

Two operational concerns that often blur together but should be kept
separate. **Retention** decides what to throw away; **backups** decide
what to keep safe.

## Retention sweeper

A built-in sweeper runs every `ARGUS_RETENTION_SWEEP_MINUTES` (default 60).
It is a singleton coordinated by an `fcntl` lock under `ARGUS_LOCK_DIR` —
only one uvicorn worker runs the sweep, even with `ARGUS_WORKERS>1`.

### What it purges

| Type | Knob | Default |
|---|---|---|
| Resource snapshots | `ARGUS_RETENTION_SNAPSHOT_DAYS` | 7 |
| Log lines | `ARGUS_RETENTION_LOG_LINE_DAYS` | 14 |
| Job epochs | `ARGUS_RETENTION_JOB_EPOCH_DAYS` | 30 |
| Other events | `ARGUS_RETENTION_EVENT_OTHER_DAYS` | 90 |
| Demo data | `ARGUS_RETENTION_DEMO_DATA_DAYS` | 1 |

### What it does **not** purge

* Batch summary rows (one per batch).
* Job summary rows (one per job).
* User accounts, projects, tokens.
* Audit log (separate retention; not user-configurable today).

A batch from 2 years ago whose detail events have been swept clean still
appears in the batch list with its final metrics — the headline data
remains visible indefinitely.

### Disable

`ARGUS_RETENTION_SWEEP_MINUTES=0` turns off the in-process sweeper. Pair
with a cron-driven external script if you want a different cadence.

### Editable from the UI

Admins can change retention days on **Settings → Admin → Retention**. The
DB row wins over the env var (see [Admin settings](admin-settings.md)).

## Built-in SQLite backups

When the DB URL points at SQLite, Argus runs an in-process backup loop
that calls SQLite's online `.backup` command (consistent under writes) on
a schedule:

| Variable | Default |
|---|---|
| `ARGUS_BACKUP_INTERVAL_H` | 6 (`0` disables the loop) |
| `ARGUS_BACKUP_KEEP_LAST_N` | 7 |

Backups land in `data/backups/` next to the live DB file. The newest N are
kept; older ones are removed.

This is also `fcntl`-coordinated, so multi-worker installs only run it
once per cycle.

For PostgreSQL the loop is a no-op — back up via `pg_dump` instead.

## Manual SQLite backup

The simplest one-shot:

```bash
docker compose exec argus sqlite3 /app/data/argus.db \
  ".backup /app/data/backups/manual-$(date +%F).db"
```

Then copy the file off the host (rclone / restic / rsync). `cp argus.db
argus.db.bak` while the server is running can produce a truncated copy —
prefer `.backup` or stop Argus first.

## PostgreSQL backups

```bash
pg_dump -h db.internal -U argus -F c argus > argus-$(date +%F).dump
```

Restore:

```bash
pg_restore -h db.internal -U argus -d argus_new argus-2026-04-26.dump
```

## What else to back up

* `./data/argus.db*` (or your Postgres dump) — the database.
* `./deploy/.env` — secrets. Without `ARGUS_JWT_SECRET` and
  `ARGUS_CONFIG_KEY` you cannot decrypt encrypted runtime config rows.

The frontend bundle and Python code are reproducible from the image — no
need to back them up.

## Retention of backups

A common rotation:

* Last 7 daily backups (the in-process default).
* Last 4 weekly snapshots (Sunday of each week, copied off-host).
* Last 6 monthly snapshots.

Tools like `restic` or `borg` handle this rotation for you.

## See also

* [Database](database.md) — SQLite vs PostgreSQL.
* [Admin settings](admin-settings.md) — UI knobs that affect retention.
