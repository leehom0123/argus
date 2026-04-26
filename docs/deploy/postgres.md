> 🌐 **English** · [中文](./postgres.zh-CN.md)

# Deploying with PostgreSQL

Argus ships with SQLite as the default backend. SQLite works well
for single-tenant developer setups and small teams. For larger deployments we
support PostgreSQL as a drop-in alternative — same migrations, same ORM layer,
no code changes in the application.

## When to switch to PostgreSQL

Switch when you hit any of the following:

- **More than ~5 concurrent reporters** writing to the same backend. SQLite
  serialises writers at the file level; under heavy ingest this shows up as
  `database is locked` warnings in the backend log.
- **More than ~100k ingested events** (roughly a few weeks of dense benchmark
  runs). SQLite handles this fine on disk, but complex dashboard queries
  (Compare with 32 batches, cross-project leaderboards) start to feel slow.
- **Multiple backend replicas** behind a load balancer. SQLite is a file; you
  need a real DBMS to share state across processes / hosts.
- **Zero-downtime backups are required.** SQLite's `.backup` loop is online but
  blocks writers briefly; Postgres has `pg_dump` and streaming replication.
- **Regulatory / ops constraints** — some environments forbid file-based
  databases and require a managed service (RDS, Cloud SQL, Aiven, ...).

If none of these apply, **stay on SQLite**. It is the zero-config default for
a reason.

## Starting a PostgreSQL container

For local development / evaluation:

```bash
docker run --rm -d \
  --name em-postgres \
  -p 5432:5432 \
  -e POSTGRES_PASSWORD=changeme \
  -e POSTGRES_DB=argus \
  -v em-postgres-data:/var/lib/postgresql/data \
  postgres:16
```

For production, use a managed instance (RDS, Cloud SQL, Aiven, Supabase) or a
dedicated host with `postgres:16` behind systemd. PostgreSQL 14+ is supported;
16 is what our CI matrix tests against.

## Installing backend Postgres drivers

The backend pulls in SQLite drivers by default. Postgres drivers live behind an
extras flag:

```bash
cd backend
pip install -e ".[postgres]"
```

This adds:

- `asyncpg>=0.30` — async runtime driver used by SQLAlchemy's async engine
- `psycopg2-binary>=2.9` — sync driver used by Alembic for DDL migrations

Both are installed together because Alembic (sync) and the request path
(async) use different paths to the same server.

## Configuring `ARGUS_DB_URL`

Point the backend at your Postgres instance with the async driver:

```bash
export ARGUS_DB_URL='postgresql+asyncpg://em_user:password@pg-host:5432/argus'
```

Then run migrations and start the backend exactly as you would for SQLite:

```bash
alembic upgrade head
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

Alembic transparently falls back to the sync `psycopg2` driver for the DDL
path; the async URL is rewritten internally.

## Migrating existing SQLite data to PostgreSQL

This is a **one-way** migration; keep a backup of the SQLite file.

Reference shape only — do not run blindly:

```bash
# 1. Dump the SQLite DB to a text SQL file.
#    The --data-only flag is important: schema goes through alembic, not dump.
sqlite3 backend/backend/data/monitor.db .dump > /tmp/em.sql

# 2. Create the Postgres schema via alembic (not via the dump).
ARGUS_DB_URL='postgresql+asyncpg://em_user:pw@pg:5432/em' alembic upgrade head

# 3. Pipe the SQLite data into Postgres. You will need to hand-edit
#    the dump to strip SQLite-only pragmas, adjust BOOLEAN literals
#    (0/1 → false/true), and re-order INSERTs for FK compatibility.
psql postgresql://em_user:pw@pg:5432/em < /tmp/em_cleaned.sql
```

A production migration tool (pgloader, or a hand-rolled Python script using
the ORM) is recommended over raw `.dump | psql` for anything beyond a few
thousand events. Write one once, keep it in `scripts/`.

## JSONB vs TEXT

Several columns store JSON (`env_snapshot_json`, `metrics_json`,
`cross_mark_weights_json`, ...). These are declared as `Text` so the same
schema works on SQLite and PostgreSQL. On Postgres we could upgrade them to
`JSONB` to enable indexed key lookups — this is a tracked roadmap item and
will require a data-migration step. The current `Text` encoding is
cross-dialect and correct; no action needed on upgrade.

## CI coverage

Every push runs the backend test suite against both SQLite and PostgreSQL 16
in parallel matrix jobs (see `.github/workflows/ci.yml`). If a migration or
query breaks on one but not the other, CI catches it before the PR merges.
