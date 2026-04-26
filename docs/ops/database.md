# Database (SQLite vs Postgres)

Argus supports two database backends: **SQLite** (default) for single-host
installs and **PostgreSQL** for multi-host or higher-throughput deployments.


## SQLite (default)

* Driver: `aiosqlite` (async).
* Mode: `WAL` (Write-Ahead Logging) — concurrent reads while a write is in
  flight, fewer "database is locked" errors than the default rollback
  journal.
* Path inside the container: `/app/data/argus.db`, mounted from the host
  via `./data:/app/data`.

```bash
ARGUS_DB_URL=sqlite+aiosqlite:////app/data/argus.db
```

### When SQLite is fine

* Single host.
* < a few thousand events per minute (a busy lab is far below this).
* Backup is the SQLite `.backup` command — consistent under writes (see
  [Backups & retention](retention.md)).

### When SQLite hurts

* Multiple Argus instances behind a load balancer (no — SQLite is
  single-node).
* Sustained burst of >5k events/sec — write throughput is the limit.
* Long backups on a multi-GB DB blocking writes.

## PostgreSQL

* Driver: `asyncpg` (async).
* Tested with PostgreSQL 14+; should work on 13.
* Migrations are run by the same `alembic upgrade head` invoked in the
  container's entrypoint.

```bash
ARGUS_DB_URL=postgresql+asyncpg://argus:secret@db.internal:5432/argus
```

### Compose snippet

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: argus
      POSTGRES_PASSWORD: secret
      POSTGRES_DB: argus
    volumes: [ "./pgdata:/var/lib/postgresql/data" ]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U argus"]
      interval: 5s
      timeout: 5s
      retries: 5

  argus:
    image: ghcr.io/leehom0123/argus:latest
    depends_on: { db: { condition: service_healthy } }
    environment:
      ARGUS_DB_URL: postgresql+asyncpg://argus:secret@db:5432/argus
      ARGUS_BASE_URL: http://localhost:8000
      ARGUS_JWT_SECRET: ${ARGUS_JWT_SECRET:?required}
    ports: ["8000:8000"]
```

### Pool sizing

Each uvicorn worker holds its own SQLAlchemy pool:

| Var | Default |
|---|---|
| `ARGUS_DB_POOL_SIZE` | per-dialect (compose sets 10) |
| `ARGUS_DB_POOL_MAX_OVERFLOW` | per-dialect (compose sets 15) |

Total connections at peak: `workers × (pool_size + max_overflow)`. With the
defaults (4 workers × 25), you sit comfortably below PostgreSQL's default
`max_connections=100`.

## Migrations

Schema changes are managed with Alembic. Versions live in
`backend/migrations/versions/` as a single linear chain.
`alembic upgrade head` runs on each container start, so an operator
never invokes it directly.

To inspect the current state:

```bash
docker compose exec argus alembic current
docker compose exec argus alembic history --verbose
```

## Switching SQLite → PostgreSQL

There is no in-place migration. Treat it as a fresh install with a one-shot
data copy:

1. Stop Argus.
2. Stand up Postgres and run `alembic upgrade head` against the new DB.
3. (Optional) For events you care about: export from SQLite, post via
   `POST /api/events/batch` against the new DB. Many installs migrate users
   + projects + tokens manually and accept losing historical events.

## See also

* [Configuration](configuration.md) — pool & connection vars.
* [Backups & retention](retention.md) — what to back up and how often.
