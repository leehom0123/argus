# Installation

Argus ships as a single Docker image (FastAPI + a pre-built Vue 3 SPA). The
reference deployment is `deploy/docker-compose.yml`.

## Prerequisites

| | Minimum | Notes |
|---|---|---|
| Docker | 24.0 | Compose v2 plugin |
| Disk | 1 GB free | SQLite + spill + frontend bundle |
| RAM | 1 GB | 4 uvicorn workers fit comfortably |
| Ports | 8000 | Default; remap with `docker compose` |
| OS | Linux / macOS | Windows works in WSL2 |

For a from-source install (developing on the backend or frontend) see
[Contributing](../contributing.md).

## Get the code

```bash
git clone https://github.com/leehom0123/argus.git
cd argus
```

## Configure environment

```bash
cd deploy
cp .env.example .env
```

Edit `.env`. The two values you almost always set:

```bash
# Generate a strong secret (≥32 bytes is enforced when ARGUS_ENV=prod)
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the result into `ARGUS_JWT_SECRET=…`. Set `ARGUS_BASE_URL` to the
public URL the browser will use (`http://localhost:8000` is fine for
local). Recommended: also set `ARGUS_CONFIG_KEY` (32+ random chars) so JWT
rotations don't touch encrypted runtime config — see
[Admin settings](../ops/admin-settings.md).

The full reference is in [Configuration](../ops/configuration.md).

## Start the stack

```bash
docker compose up -d --build
```

The image builds in two stages (Node for the frontend, Python for the
backend), runs `alembic upgrade head`, then starts uvicorn with 4 workers.
First boot takes a few minutes; subsequent restarts are seconds.

## Verify

```bash
docker compose ps          # service should be healthy
curl http://localhost:8000/health
# {"status":"ok"}
```

Open <http://localhost:8000>. The login page appears. Click **Register** —
**the first user to register is automatically promoted to admin**.

## What you get

* `./data/argus.db` — SQLite database (WAL mode), persists on the host volume
* `./data/backups/` — built-in SQLite snapshots (every `ARGUS_BACKUP_INTERVAL_H` hours)
* `docker compose logs -f` — application logs

## Update

```bash
git pull
cd deploy
docker compose up -d --build
```

Alembic upgrades run automatically on each container start
(`deploy/entrypoint.sh`).

## Next

* [First run](first-run.md) — register, mint a token, see live data.
* [Connect a training job](connect-training.md) — push events from Python.
