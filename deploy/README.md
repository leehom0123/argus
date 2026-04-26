# Deployment (Docker)

Two-stage image: node builds the Vue SPA, Python serves it together with the
FastAPI backend. Frontend dist is copied to `backend/frontend/dist`, which is
where `backend/backend/app.py` mounts StaticFiles at `/`.

## Prereqs

- Docker 24+ with Compose v2 (`docker compose`, not `docker-compose`).
- ~1 GB disk for the image and its layers.

## Local smoke

```bash
cd deploy
cp .env.example .env
# Generate a real JWT secret:
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
# Edit .env and paste the value over the ARGUS_JWT_SECRET placeholder.

docker compose build                 # first build: ~3-5 min
docker compose up -d
curl -fsS http://localhost:8000/health          # => {"status":"ok"}
curl -s  http://localhost:8000/ | head          # => <!DOCTYPE html>
docker compose logs --tail=60
docker compose down                              # stops and removes
```

## Remote deploy (SSH-key auth)

When the production target terminates TLS in front of this stack (e.g. nginx
on the same host), the container only needs to expose `:8000` on localhost
for the proxy to upstream against.

```bash
# From a workstation, one-shot deploy. Every REMOTE_* var is required:
REMOTE_HOST=argus.example.com REMOTE_PORT=22 REMOTE_USER=deploy \
    REMOTE_PATH=/opt/argus \
    ./deploy.sh
```

After it finishes:

```bash
curl -k https://argus.example.com/health
```

## Deploying to a password-auth server

For servers reachable only via password (no SSH key, and `sshpass` is not
installed locally), use the paramiko-based deploy script `deploy/deploy.py`.
It does the same job as `deploy.sh` but uses pure Python (paramiko + PyYAML)
for SSH/SCP, so it works on any laptop with `pip install paramiko pyyaml`.

```bash
# 1. One-time setup: copy the template, fill in credentials.
cp deploy/server.yaml.example deploy/server.yaml
# Edit deploy/server.yaml — replace REPLACE_ME placeholders (host, user,
# password, project_root, public_url). deploy/server.yaml is gitignored.

# 2. One-time setup on the remote: pre-stage the .env file with secrets.
ssh -p <port> <user>@<host>           # use the credentials from server.yaml
mkdir -p /opt/argus/deploy
nano   /opt/argus/deploy/.env.production
# Set ARGUS_JWT_SECRET, ARGUS_DB_URL, SMTP_*, ARGUS_FEISHU_WEBHOOK, etc.
# Path is referenced from deploy/server.yaml under remote.deploy.env_file.

# 3. Deploy.
pip install paramiko pyyaml          # if not already installed
python deploy/deploy.py              # uses activate: from server.yaml

# Other useful invocations:
SERVER=staging python deploy/deploy.py         # override active server
python deploy/deploy.py --clean                # rm -rf remote first (DESTRUCTIVE)
python deploy/deploy.py --no-build             # use cached image
python deploy/deploy.py --dry-run              # print plan, no I/O
```

The script tar's the repo (excluding `node_modules`, `frontend/dist`,
`deploy/data`, `deploy/.env*`, `deploy/server.yaml`), scp's to
`/tmp/argus-deploy.<pid>.tar.gz` on the remote, untars under
`remote.project_root`, copies the pre-staged `.env.production` into
`deploy/.env`, then runs `docker compose up -d --build` and curls
`/health`. End-to-end: ~3-5 minutes for a clean build.

## Volumes

- `./data` — SQLite database plus any future spill directories. Back this up
  periodically; everything else is reproducible from the image.

## Reverse proxy (reference only)

The production reverse proxy lives on the server; copy hints from
`nginx.snippet.conf` if setting up a new host. Remember to disable
`proxy_buffering` on the SSE route and to scrub the `?token=` query
parameter out of access logs.

## Client install

Reporter clients running on other hosts install the companion library from
PyPI on connected machines, or via the in-tree wheel for air-gapped servers:

```bash
# Connected machine (recommended):
pip install argus-reporter

# Air-gapped: copy client/dist/*.whl across and:
pip install argus_reporter-0.1.2-py3-none-any.whl

# Editable install for SDK development:
pip install -e ./client[dev]
```

See `client/README.md` for the v0.3 context-manager quickstart.
