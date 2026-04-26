# Docker deployment

The reference deployment is a single container image built from
`deploy/Dockerfile` via `deploy/docker-compose.yml`. The container holds the
Vue 3 SPA (statically served) and the FastAPI backend (4 uvicorn workers by
default). Data is a host-mounted volume at `./data`.

## Image

```
ghcr.io/leehom0123/argus:latest
```

The image is multi-stage:

1. Node builds the frontend with `pnpm build`.
2. Python installs the backend with the standard `pip install -e backend`.
3. The frontend `dist/` is copied alongside.

`docker compose build` builds locally; pushed images live on GHCR.

## Compose file

```yaml
# deploy/docker-compose.yml (excerpt)
services:
  argus:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    image: ghcr.io/leehom0123/argus:latest
    container_name: argus
    restart: unless-stopped
    ports: ["8000:8000"]
    environment:
      ARGUS_JWT_SECRET: ${ARGUS_JWT_SECRET:?required}
      ARGUS_DB_URL: ${ARGUS_DB_URL:-sqlite+aiosqlite:////app/data/argus.db}
      ARGUS_BASE_URL: ${ARGUS_BASE_URL:-https://argus.example.com}
      ARGUS_LOG_LEVEL: ${ARGUS_LOG_LEVEL:-info}
      ARGUS_ENV: ${ARGUS_ENV:-prod}
      # …SMTP, retention, OAuth env vars (see deploy/.env.example)…
    volumes:
      - ./data:/app/data
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
```

The `./data` host folder holds the SQLite DB and any spill / backup
artifacts. Back this up — see [Backups & retention](retention.md).

## Lifecycle

```bash
cd deploy
cp .env.example .env                          # edit secrets

docker compose up -d --build                  # build + start

docker compose ps                             # health check
docker compose logs -f                        # tail logs
docker compose exec argus alembic current     # current DB revision
docker compose down                           # stop, keep data
docker compose down -v                        # ⚠ stop and DELETE data
```

The container's entrypoint (`deploy/entrypoint.sh`) runs Alembic migrations
before exec-ing uvicorn — there is no separate migration step.

## Behind a reverse proxy

The shipped reference is `deploy/nginx.snippet.conf`. The key items it
captures:

* The SSE endpoint needs `proxy_buffering off` and a long `proxy_read_timeout`
  so live events are not coalesced.
* `proxy_http_version 1.1` and an empty `Connection` header for keepalive.
* Standard `X-Forwarded-*` headers so the backend sees the real client.

Set `ARGUS_BASE_URL=https://your.domain` in `.env` so generated email
links and the CORS allow-list use the right host.

## Scaling

* `ARGUS_WORKERS=N` controls uvicorn workers (read by the entrypoint).
  Default 4. Set to 1 for the SQLite dev loop; scale toward `N_CORES` when
  running on Postgres.
* In-process singletons (retention sweep, SQLite backup, watchdog,
  notifications dispatcher) are coordinated with fcntl advisory locks
  under `ARGUS_LOCK_DIR` (default `/tmp`) so only one worker runs each.
* For PostgreSQL, see [Database](database.md).

## Build vs pull

`docker compose up --build` rebuilds locally — useful for development or
behind a firewall that blocks GHCR. Drop `--build` to pull the published
image.

## See also

* [Configuration](configuration.md) — every env var.
* [Database](database.md) — SQLite vs PostgreSQL.
* [Argus Agent](argus-agent.md) — host-side companion daemon (in Sibyl).
