# Multi-stage build for Argus.
#
# Stage 1 builds the Vue frontend into static assets. Stage 2 installs the
# FastAPI backend (editable for dev parity), then copies the built frontend
# so that FastAPI's StaticFiles mount picks it up at runtime.
#
# Build context must be the repository root so `backend/`, `frontend/`, and
# `schemas/` are all copyable (see deploy/docker-compose.yml `context: ..`).

# ---------------------------------------------------------------------------
# Stage 1 — frontend build
# ---------------------------------------------------------------------------
FROM node:25-alpine AS frontend-build
WORKDIR /build

# Use npm (lighter than corepack-pnpm) so the image works whether or not a
# pnpm-lock.yaml is present. We copy both lockfiles (if they exist) and
# select at install time.
COPY frontend/package.json ./
COPY frontend/package-lock.json* frontend/pnpm-lock.yaml* ./

RUN if [ -f pnpm-lock.yaml ]; then \
        corepack enable && pnpm install --frozen-lockfile; \
    elif [ -f package-lock.json ]; then \
        npm ci; \
    else \
        npm install; \
    fi

COPY frontend/ ./

RUN if [ -f pnpm-lock.yaml ]; then pnpm run build; else npm run build; fi


# ---------------------------------------------------------------------------
# Stage 2 — backend runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# System packages: build-essential for argon2-cffi wheels on uncommon archs,
# curl for the healthcheck. libpq-dev is included so future Postgres support
# doesn't need a new base layer.
# Swap apt mirror to Aliyun (CN networks) — handle both legacy
# /etc/apt/sources.list AND deb822 /etc/apt/sources.list.d/debian.sources.
RUN (sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' \
        /etc/apt/sources.list 2>/dev/null || true) \
 && (sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true) \
 && (sed -i 's|http://security.debian.org|http://mirrors.aliyun.com|g' \
        /etc/apt/sources.list 2>/dev/null || true) \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first, using only pyproject.toml, so the layer caches
# independently of source changes.
COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/README.md ./backend/README.md
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30,<0.35" \
        "python-multipart>=0.0.6" \
        "sqlalchemy[asyncio]>=2.0" \
        "aiosqlite" \
        "pydantic>=2.5" \
        "pydantic-settings>=2.0" \
        "email-validator>=2.0" \
        "jsonschema>=4" \
        "pyyaml" \
        "httpx" \
        "argon2-cffi>=23.1" \
        "pyjwt>=2.8" \
        "aiosmtplib>=3.0" \
        "jinja2>=3.1" \
        "alembic>=1.13"

# Backend source. Resulting layout inside the image:
#   /app/backend/                   <- project root (alembic.ini, migrations/, pyproject)
#   /app/backend/backend/app.py     <- FastAPI module (`backend.app:app`)
#   /app/backend/frontend/dist/     <- built frontend (BACKEND_DIR.parent.parent/frontend/dist)
#   /app/schemas/                   <- event_v1.json etc
#   /app/data/                      <- SQLite volume mount target
COPY backend/ ./backend/

# Install backend as editable so `backend.app:app` resolves.
# Include Postgres drivers so the same image supports both DB backends —
# users pick via ARGUS_DB_URL at runtime. Tiny size impact (~5 MB); not
# worth maintaining two image tags.
RUN pip install --no-cache-dir -e ./backend \
 && pip install --no-cache-dir asyncpg psycopg2-binary

# Drop the built frontend exactly where backend/backend/app.py expects it:
# FRONTEND_DIST_PATH = BACKEND_DIR.parent.parent / "frontend" / "dist"
# BACKEND_DIR = /app/backend/backend  =>  .parent = /app/backend  =>  .parent = /app
# So the resolved path is /app/frontend/dist (two parents up from BACKEND_DIR).
COPY --from=frontend-build /build/dist ./frontend/dist

# Event schema (not strictly required by the runtime app today, but ships with
# the image for anyone curling /schemas/event_v1.json via a future route).
COPY schemas/ ./schemas/

# Entrypoint: run alembic migrations (operator-driven, per backend/README.md)
# then start uvicorn. Kept outside the backend dir so the app layer doesn't
# have to be rebuilt when the entrypoint changes.
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# SQLite data dir; the compose file binds a host volume here.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
