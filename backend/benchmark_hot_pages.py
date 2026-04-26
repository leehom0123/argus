"""Benchmark hot API endpoints with seeded data.

Seeds ~100 batches, ~1000 jobs, ~10k events, ~5k resource snapshots into
a scratch SQLite DB, boots the FastAPI app in-process, then times each
hot endpoint with N repetitions and reports p50/p95/p99.

Usage
-----
    cd backend
    python benchmark_hot_pages.py                # default 50 reps
    python benchmark_hot_pages.py --reps 100     # more samples
    python benchmark_hot_pages.py --quick        # smaller seed

The script writes a CSV (``benchmark_hot_pages.csv``) and prints a
markdown table to stdout so `run > perf_before.csv` etc. makes sense.

This is intentionally a one-file utility — it's meant as a repeatable
lane measurement tool for Team Perf, not a permanent test fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from pathlib import Path

# Must happen before any `backend` import so Settings pick up the test DB.
BENCH_DB = Path(__file__).parent / "data" / "perf_bench.db"
BENCH_DB.parent.mkdir(parents=True, exist_ok=True)
if BENCH_DB.exists():
    BENCH_DB.unlink()

os.environ["ARGUS_DB_URL"] = f"sqlite+aiosqlite:///{BENCH_DB}"
os.environ.setdefault(
    "ARGUS_JWT_SECRET",
    "bench-secret-32-bytes-minimum-fixture-value",
)
os.environ.setdefault("ARGUS_SKIP_DEMO_SEED", "1")
os.environ.setdefault("ARGUS_WATCHDOG_ENABLED", "false")
os.environ.setdefault("ARGUS_RETENTION_SWEEP_MINUTES", "0")
os.environ.setdefault("ARGUS_BACKUP_INTERVAL_H", "0")
os.environ.setdefault("ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED", "false")

from datetime import datetime, timedelta, timezone  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def seed(
    n_batches: int,
    n_jobs_per_batch: int,
    n_epochs_per_job: int,
    n_hosts: int,
    n_snapshots_per_host: int,
) -> dict:
    """Populate the scratch DB directly via SQLAlchemy (fast path)."""
    from backend import models  # noqa: F401
    from backend.db import Base, SessionLocal, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    rng = random.Random(42)
    now = datetime.now(timezone.utc)

    # Single tester admin user so the API boots an authenticated client.
    # We create it through the normal register path in `main` below; here
    # we just seed the domain tables directly.

    batch_ids: list[str] = []
    job_pairs: list[tuple[str, str]] = []  # (batch_id, job_id)
    host_names = [f"host-{i:02d}" for i in range(n_hosts)]

    async with SessionLocal() as session:
        # Batches + jobs
        for b in range(n_batches):
            bid = f"batch-{b:04d}"
            batch_ids.append(bid)
            proj = f"proj-{b % 5}"
            status = rng.choice(
                ["done", "done", "done", "running", "failed"]
            )
            start = now - timedelta(hours=rng.randint(0, 48))
            end = start + timedelta(minutes=rng.randint(5, 120))
            session.add(
                models.Batch(
                    id=bid,
                    project=proj,
                    experiment_type="forecast",
                    host=rng.choice(host_names),
                    user="tester",
                    n_total=n_jobs_per_batch,
                    n_done=n_jobs_per_batch if status == "done" else 0,
                    n_failed=0,
                    status=status,
                    start_time=_iso(start),
                    end_time=_iso(end) if status != "running" else None,
                    owner_id=1,
                    is_deleted=False,
                )
            )
            for j in range(n_jobs_per_batch):
                jid = f"job-{b:04d}-{j:03d}"
                job_pairs.append((bid, jid))
                jstatus = "done" if status == "done" else rng.choice(
                    ["done", "done", "running", "failed"]
                )
                jstart = start + timedelta(minutes=rng.randint(0, 30))
                jend = jstart + timedelta(minutes=rng.randint(1, 60))
                metrics = json.dumps(
                    {
                        "MSE": round(rng.uniform(0.1, 0.5), 4),
                        "MAE": round(rng.uniform(0.1, 0.5), 4),
                        "gpu_count": rng.choice([1, 1, 1, 2, 4]),
                    }
                )
                session.add(
                    models.Job(
                        id=jid,
                        batch_id=bid,
                        model=rng.choice(
                            ["transformer", "dlinear", "patchtst"]
                        ),
                        dataset=rng.choice(["etth1", "etth2", "electricity"]),
                        status=jstatus,
                        start_time=_iso(jstart),
                        end_time=_iso(jend) if jstatus != "running" else None,
                        elapsed_s=(jend - jstart).seconds,
                        metrics=metrics,
                        is_idle_flagged=False,
                    )
                )

        # Events: job_start + job_epochs + job_done for each job
        event_uuid = uuid.uuid4
        for bid, jid in job_pairs:
            base = now - timedelta(hours=rng.randint(0, 24))
            session.add(
                models.Event(
                    batch_id=bid,
                    job_id=jid,
                    event_type="job_start",
                    timestamp=_iso(base),
                    schema_version="1.1",
                    data=None,
                    event_id=str(event_uuid()),
                )
            )
            for e in range(n_epochs_per_job):
                session.add(
                    models.Event(
                        batch_id=bid,
                        job_id=jid,
                        event_type="job_epoch",
                        timestamp=_iso(base + timedelta(seconds=e * 30)),
                        schema_version="1.1",
                        data=json.dumps(
                            {
                                "epoch": e + 1,
                                "train_loss": round(rng.uniform(0.1, 1.0), 4),
                                "val_loss": round(rng.uniform(0.1, 1.0), 4),
                                "lr": 1e-4,
                            }
                        ),
                        event_id=str(event_uuid()),
                    )
                )
            session.add(
                models.Event(
                    batch_id=bid,
                    job_id=jid,
                    event_type="job_done",
                    timestamp=_iso(
                        base + timedelta(seconds=n_epochs_per_job * 30 + 5)
                    ),
                    schema_version="1.1",
                    data=None,
                    event_id=str(event_uuid()),
                )
            )

        # Resource snapshots across hosts
        for host in host_names:
            for s in range(n_snapshots_per_host):
                ts = now - timedelta(minutes=s)
                session.add(
                    models.ResourceSnapshot(
                        host=host,
                        timestamp=_iso(ts),
                        gpu_util_pct=rng.uniform(0, 100),
                        gpu_mem_mb=rng.uniform(0, 24000),
                        gpu_mem_total_mb=24000,
                        gpu_temp_c=rng.uniform(40, 80),
                        cpu_util_pct=rng.uniform(0, 100),
                        ram_mb=rng.uniform(0, 64000),
                        ram_total_mb=64000,
                        disk_free_mb=500_000,
                        proc_cpu_pct=rng.uniform(0, 50),
                        proc_ram_mb=int(rng.uniform(500, 8000)),
                        proc_gpu_mem_mb=int(rng.uniform(500, 16000)),
                        batch_id=rng.choice(batch_ids),
                    )
                )

        await session.commit()

    return {
        "batch_ids": batch_ids,
        "host_names": host_names,
        "job_pairs": job_pairs,
    }


async def register_and_token(client: AsyncClient) -> str:
    # Register first user (auto-admin), then mint a reporter token.
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": "tester",
            "email": "tester@example.com",
            "password": "password123",
        },
    )
    assert reg.status_code == 201, reg.text
    login = await client.post(
        "/api/auth/login",
        json={"username_or_email": "tester", "password": "password123"},
    )
    assert login.status_code == 200, login.text
    jwt = login.json()["access_token"]
    tok = await client.post(
        "/api/tokens",
        json={"name": "bench", "scope": "reporter"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert tok.status_code == 201, tok.text
    return jwt


async def time_endpoint(
    client: AsyncClient,
    path: str,
    reps: int,
    headers: dict | None = None,
) -> list[float]:
    # Warm-up
    r = await client.get(path, headers=headers)
    if r.status_code >= 500:
        print(f"  !! {path} returned {r.status_code}: {r.text[:200]}")
    samples: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        await client.get(path, headers=headers)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def pct(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    idx = max(0, min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1)))))
    return s[idx]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="benchmark_hot_pages.csv")
    args = ap.parse_args()

    if args.quick:
        seed_cfg = dict(
            n_batches=20,
            n_jobs_per_batch=5,
            n_epochs_per_job=10,
            n_hosts=3,
            n_snapshots_per_host=60,
        )
    else:
        seed_cfg = dict(
            n_batches=100,
            n_jobs_per_batch=10,
            n_epochs_per_job=20,
            n_hosts=5,
            n_snapshots_per_host=120,
        )

    print(f"seeding: {seed_cfg}")
    seeded = await seed(**seed_cfg)
    batch_ids = seeded["batch_ids"]
    host_names = seeded["host_names"]
    job_pairs = seeded["job_pairs"]
    print(
        f"seeded: {len(batch_ids)} batches, {len(job_pairs)} jobs, "
        f"{len(host_names)} hosts"
    )

    from backend.app import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            jwt = await register_and_token(client)
            headers = {"Authorization": f"Bearer {jwt}"}

            # Pick representative ids
            b0 = batch_ids[0]
            b_last = batch_ids[-1]
            compare_ids = ",".join(batch_ids[:4])
            jbid, jjid = job_pairs[0]
            host0 = host_names[0]

            endpoints = [
                ("/api/dashboard", None),
                ("/api/batches?scope=all", None),
                (f"/api/batches/{b0}", None),
                (f"/api/batches/{b0}/jobs", None),
                (f"/api/batches/{b_last}/jobs", None),
                (f"/api/jobs/{jbid}/{jjid}/epochs", None),
                (f"/api/batches/{b0}/jobs/eta-all", None),
                (f"/api/compare?batches={compare_ids}", None),
                ("/api/projects", None),
                ("/api/stats/gpu-hours-by-user?days=30", None),
                (f"/api/hosts/{host0}/timeseries?since=now-1h", None),
            ]

            rows: list[dict] = []
            for path, hdr_override in endpoints:
                h = hdr_override or headers
                samples = await time_endpoint(
                    client, path, reps=args.reps, headers=h
                )
                row = {
                    "endpoint": path,
                    "n": len(samples),
                    "p50_ms": round(statistics.median(samples), 1),
                    "p95_ms": round(pct(samples, 95), 1),
                    "p99_ms": round(pct(samples, 99), 1),
                    "mean_ms": round(statistics.mean(samples), 1),
                }
                rows.append(row)

    # Emit markdown table
    print("\n| endpoint | p50 ms | p95 ms | p99 ms | mean ms |")
    print("|---|---:|---:|---:|---:|")
    for r in rows:
        print(
            f"| `{r['endpoint']}` | {r['p50_ms']} | {r['p95_ms']} "
            f"| {r['p99_ms']} | {r['mean_ms']} |"
        )

    # CSV
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as f:
        f.write("endpoint,n,p50_ms,p95_ms,p99_ms,mean_ms\n")
        for r in rows:
            f.write(
                f"{r['endpoint']},{r['n']},{r['p50_ms']},"
                f"{r['p95_ms']},{r['p99_ms']},{r['mean_ms']}\n"
            )
    print(f"\nwrote {out}")

    # Cleanup scratch DB so reruns are reproducible.
    from backend.db import engine

    await engine.dispose()
    if BENCH_DB.exists():
        BENCH_DB.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
