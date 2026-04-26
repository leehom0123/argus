#!/usr/bin/env bash
# Ship Argus to the remote host, build, and bring it up.
#
# Usage: every connection field must be supplied via env vars; this script
# refuses to run with built-in defaults so an operator can't accidentally
# deploy to the wrong host.
#
#   REMOTE_HOST=argus.example.com \
#       REMOTE_PORT=22 \
#       REMOTE_USER=deploy \
#       REMOTE_PATH=/opt/argus \
#       ./deploy/deploy.sh
#
# For password-auth servers prefer the Python entry point at
# deploy/deploy.py — it reads connection details from deploy/server.yaml
# (gitignored) so credentials never live in your shell history.
#
# Requires: rsync OR scp+tar on the local side; ssh + docker compose v2 on
# the remote side. If the remote only has docker-compose v1 (`docker-compose`),
# the final `docker compose` calls will fail and need manual fixup.

set -euo pipefail

: "${REMOTE_HOST:?REMOTE_HOST is required (no default; set to your server hostname)}"
: "${REMOTE_USER:?REMOTE_USER is required (no default; set to the SSH login user)}"
: "${REMOTE_PATH:?REMOTE_PATH is required (no default; e.g. /opt/argus)}"
REMOTE_PORT="${REMOTE_PORT:-22}"

# --- locate repo root ------------------------------------------------------
REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel 2>/dev/null \
    || realpath "$(dirname "$0")/..")"
cd "$REPO_ROOT"
echo "[deploy] repo root: $REPO_ROOT"
echo "[deploy] target:    $REMOTE_USER@$REMOTE_HOST:$REMOTE_PORT -> $REMOTE_PATH"

# --- tar up the bits the container build actually needs --------------------
TARBALL="$(mktemp -t em-deploy.XXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT

tar \
    --exclude='**/node_modules' \
    --exclude='**/__pycache__' \
    --exclude='**/.git' \
    --exclude='**/.pytest_cache' \
    --exclude='frontend/dist' \
    --exclude='backend/data' \
    --exclude='deploy/data' \
    --exclude='deploy/.env' \
    -czf "$TARBALL" \
    backend frontend client schemas deploy README.md README_ZH.md 2>/dev/null || \
tar \
    --exclude='**/node_modules' \
    --exclude='**/__pycache__' \
    --exclude='**/.git' \
    --exclude='**/.pytest_cache' \
    --exclude='frontend/dist' \
    --exclude='backend/data' \
    --exclude='deploy/data' \
    --exclude='deploy/.env' \
    -czf "$TARBALL" \
    backend frontend client schemas deploy README.md

echo "[deploy] tarball: $(du -h "$TARBALL" | cut -f1)"

# --- upload ----------------------------------------------------------------
REMOTE_TMP=/tmp/em-deploy.$$.tar.gz
scp -P "$REMOTE_PORT" "$TARBALL" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_TMP}"

# --- remote untar + build + up ---------------------------------------------
ssh -p "$REMOTE_PORT" "${REMOTE_USER}@${REMOTE_HOST}" bash -s <<REMOTE_SCRIPT
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
    echo "!! docker not found on remote. Install it first, then re-run deploy.sh."
    echo "   Ubuntu/Debian quick install:"
    echo "     curl -fsSL https://get.docker.com | sudo sh"
    echo "     sudo usermod -aG docker \$USER"
    exit 2
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "!! docker compose v2 plugin not found. Install docker-compose-plugin."
    exit 2
fi

mkdir -p "${REMOTE_PATH}"
cd "${REMOTE_PATH}"
tar -xzf "${REMOTE_TMP}"
rm -f "${REMOTE_TMP}"

cd deploy
if [ ! -f .env ]; then
    cp .env.example .env
    SECRET=\$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
    # Replace the placeholder with the freshly generated secret. The sentinel
    # is the literal string between the angle brackets in .env.example.
    sed -i "s|<generate.*>|\${SECRET}|" .env
    echo "[remote] fresh .env generated. Edit SMTP + Feishu later if needed."
fi

mkdir -p data

echo "[remote] docker compose build ..."
docker compose -f docker-compose.yml build

echo "[remote] docker compose up -d ..."
docker compose -f docker-compose.yml up -d

sleep 5
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs --tail=50

echo "[remote] health check ..."
curl -fsS http://localhost:8000/health || { echo "health failed"; exit 3; }
echo "[remote] OK"
REMOTE_SCRIPT

echo ""
echo "[deploy] Done. Verify from your laptop:"
echo "   curl -k https://<your-argus-host>/health"
