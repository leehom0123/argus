#!/usr/bin/env bash
# Container entrypoint: run Alembic migrations, then exec the given command.
#
# The backend contract (backend/README.md) requires migrations to be run
# explicitly by the operator before uvicorn starts — the app itself does NOT
# run them in-process. Doing it here keeps that contract while still giving
# `docker compose up -d` a one-shot bring-up.

set -euo pipefail

cd /app/backend

# ARGUS_DB_URL defaults to the baked-in sqlite path when unset, so alembic
# will pick up whatever docker-compose passes in as env.
echo "[entrypoint] running alembic upgrade head ..."
alembic upgrade head

# Multi-worker uvicorn: one process handles ingest, 4-way concurrency by
# default. Operators can shrink back to 1 for the SQLite dev loop, or grow
# to ~N_CORES for a Postgres-backed prod install. In-process singletons
# (retention sweep, SQLite backup cron, watchdog) are fcntl-guarded in
# backend.app so only one worker actually runs them — see
# docs/deploy/scaling.md for the full model.
#
# We inject --workers only when the CMD is a bare uvicorn call that
# doesn't already set it, so a debug override like
# `docker compose run argus uvicorn ... --reload` still wins.
ARGUS_WORKERS="${ARGUS_WORKERS:-4}"
if [ "$#" -ge 1 ] && [ "$1" = "uvicorn" ] && ! printf '%s\n' "$@" | grep -qE '^--workers$|^--workers='; then
    set -- "$@" --workers "${ARGUS_WORKERS}"
fi

echo "[entrypoint] starting: $*"
exec "$@"
