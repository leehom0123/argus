"""FastAPI application factory for the experiment monitor backend."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional
from urllib.parse import unquote, urlparse

# fcntl is POSIX-only. On non-POSIX hosts the singleton-lock helper
# degrades to "everyone wins" — acceptable because ARGUS_WORKERS>1 is
# only supported on Linux/macOS containers; the dev loop on Windows runs
# with a single worker anyway.
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - windows
    _fcntl = None  # type: ignore[assignment]

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend import __version__
from backend.api import admin as admin_api
from backend.api import admin_config as admin_config_api
from backend.api import admin_security as admin_security_api
from backend.api import agents as agents_api
from backend.api import artifacts as artifacts_api
from backend.api import auth as auth_api
from backend.api import batches as batches_api
from backend.api import compare as compare_api
from backend.api import dashboard as dashboard_api
from backend.api import events as events_api
from backend.api import events_stream as events_stream_api
from backend.api import sse_multiplex as sse_multiplex_api
from backend.api import jobs as jobs_api
from backend.api import me as me_api
from backend.api import meta as meta_api
from backend.api import oauth as oauth_api
from backend.api import batch_email_subscription as batch_email_subscription_api
from backend.api import email_admin as email_admin_api
from backend.api import notifications_email as notifications_email_api
from backend.api import notifications as notifications_api
from backend.api import (
    project_notification_recipients as project_notification_recipients_api,
)
from backend.api import pins as pins_api
from backend.api import preferences as preferences_api
from backend.api import projects as projects_api
from backend.api import public as public_api
from backend.api import hosts as hosts_api
from backend.api import resources as resources_api
from backend.api import shares as shares_api
from backend.api import stars as stars_api
from backend.api import stats as stats_api
from backend.api import studies as studies_api
from backend.api import tokens as tokens_api
from backend.auth.jwt import start_blacklist_purge_task
from backend.services.jwt_rotation import (
    hydrate_cache as _jwt_rotation_hydrate_cache,
    start_rotation_sweep_task,
)
from backend.config import get_settings
from backend.db import SessionLocal, dispose_db, init_db
from backend.demo import seed_demo
from backend.notifications import FeishuNotifier, load_rules
from backend.notifications.watchdog import watchdog_loop

log = logging.getLogger(__name__)


BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_RULES_PATH = BACKEND_DIR / "config" / "notifications.yaml"
FRONTEND_DIST_PATH = BACKEND_DIR.parent.parent / "frontend" / "dist"


# Directory holding worker-coordination lock files. Lives in /tmp by default
# because that's guaranteed writable in the container; tests can redirect to
# pytest's ``tmp_path`` via the ``ARGUS_LOCK_DIR`` env var.
_LOCK_DIR = Path(os.environ.get("ARGUS_LOCK_DIR", "/tmp"))

# Lock handles are kept alive for the lifetime of the process. fcntl(2)
# releases the advisory flock when the fd is closed, so we MUST hold a
# reference — don't let garbage collection reap it.
_singleton_lock_handles: dict[str, object] = {}


def _try_singleton_lock(name: str) -> bool:
    """Acquire an advisory exclusive lock on ``<LOCK_DIR>/argus-<name>.lock``.

    Returns True when this worker process won the lock (and should run the
    guarded singleton loop), False when another worker already holds it.
    On non-POSIX hosts without ``fcntl`` we optimistically return True —
    ARGUS_WORKERS>1 is only supported on Linux/macOS so this path is
    unreachable in any real deployment.

    The lock is held for the lifetime of the process; we never manually
    release it. The kernel drops the flock automatically on process exit
    (including SIGKILL), so a crashed worker never permanently locks out
    its replacement.
    """
    if _fcntl is None:  # pragma: no cover - windows
        return True
    try:
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("singleton_lock: cannot create %s: %s", _LOCK_DIR, exc)
        return True  # fail-open: better to over-run than silently skip
    path = _LOCK_DIR / f"argus-{name}.lock"
    try:
        fh = open(path, "w")
    except Exception as exc:  # noqa: BLE001
        log.warning("singleton_lock: cannot open %s: %s", path, exc)
        return True
    try:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        log.info("singleton_lock: %s held by another worker; skipping", name)
        return False
    except OSError as exc:  # pragma: no cover - filesystem edge cases
        log.warning("singleton_lock: flock(%s) failed: %s", path, exc)
        fh.close()
        return True
    # Keep the handle alive so the kernel doesn't drop our flock.
    _singleton_lock_handles[name] = fh
    log.info("singleton_lock: %s acquired (pid=%d)", name, os.getpid())
    return True


# Matches ``token=<anything-not-& -whitespace- or quote>`` so we don't
# accidentally swallow the trailing HTTP version / headers in the log
# record. Both ``em_live_...``/``em_view_...`` API tokens and compact JWTs
# are captured because neither contains ``&``, whitespace or double quote.
_TOKEN_QUERY_PAT = re.compile(r'token=[^&\s"]+')


class _TokenRedactFilter(logging.Filter):
    """Rewrite ``token=<value>`` to ``token=REDACTED`` in log records.

    Attached to the ``uvicorn.access`` logger because uvicorn's default
    access format includes the full request line (path + query string)
    via ``record.args``, which would otherwise leak JWT query-param
    tokens into the log file on SSE subscribes (M4 in the Phase-3
    post-review notes). The filter is a safety net — production
    deployments should still strip the query parameter at the reverse
    proxy.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        args = record.args
        if not args:
            return True
        # Only rewrite string args. Uvicorn's access format passes the status
        # code as int into a `%d` slot; coercing it to str via the regex path
        # triggers `TypeError: %d format: a real number is required, not str`
        # on every request.
        if isinstance(args, dict):
            record.args = {
                k: (_TOKEN_QUERY_PAT.sub("token=REDACTED", v) if isinstance(v, str) else v)
                for k, v in args.items()
            }
        elif isinstance(args, tuple):
            record.args = tuple(
                _TOKEN_QUERY_PAT.sub("token=REDACTED", a) if isinstance(a, str) else a
                for a in args
            )
        return True


def _install_access_log_redaction() -> None:
    """Install the ``?token=`` redaction filter on the uvicorn access logger.

    Idempotent — safe to call from repeated `create_app()` invocations
    (the test suite builds a fresh app per fixture).
    """
    logger = logging.getLogger("uvicorn.access")
    already = any(
        isinstance(f, _TokenRedactFilter) for f in logger.filters
    )
    if not already:
        logger.addFilter(_TokenRedactFilter())


def _configure_notifications(app: FastAPI) -> None:
    """Load notification rules + build channel instances from env/yaml."""
    rules_path = os.environ.get(
        "ARGUS_NOTIFICATION_RULES", str(DEFAULT_RULES_PATH)
    )
    app.state.notification_rules = load_rules(rules_path)

    channels: dict[str, object] = {}
    feishu_url = os.environ.get("ARGUS_FEISHU_WEBHOOK")
    # Also allow channel config inside the YAML under ``channels.feishu.webhook_url``.
    if not feishu_url and Path(rules_path).exists():
        try:
            import yaml

            with open(rules_path, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
            feishu_url = (
                (doc.get("channels") or {}).get("feishu", {}).get("webhook_url")
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not parse channels from %s: %s", rules_path, exc)
    if feishu_url and "REPLACE_ME" not in feishu_url:
        channels["feishu"] = FeishuNotifier(feishu_url)
    app.state.notification_channels = channels


def _cors_origins(base_url: str) -> list[str]:
    """Derive the CORS allow-list from config.

    Always allow the Vite dev server. In addition, allow the base URL and
    the canonicalised scheme+host variants so a prod deployment with HTTPS
    isn't blocked by a trailing slash mismatch.
    """
    origins = {"http://localhost:5173", "http://localhost:8000"}
    try:
        parsed = urlparse(base_url)
        if parsed.scheme and parsed.netloc:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
    except Exception:
        pass
    return sorted(origins)


def _sqlite_path_from_url(db_url: str) -> Path | None:
    """Return the local file path behind a ``sqlite+aiosqlite:///...`` URL.

    Returns None when the URL targets a non-file SQLite DB (``:memory:``)
    or a non-sqlite backend — the backup loop is a no-op in those cases.
    """
    if not db_url.startswith("sqlite"):
        return None
    # Manual parse: urlparse chokes on the ``+driver`` fragment.
    prefix = "sqlite+aiosqlite:///"
    if db_url.startswith(prefix):
        tail = db_url[len(prefix):]
    elif db_url.startswith("sqlite:///"):
        tail = db_url[len("sqlite:///"):]
    else:
        return None
    tail = unquote(tail)
    if tail.strip() in ("", ":memory:"):
        return None
    return Path(tail)


def _perform_sqlite_backup(
    src: Path, backup_dir: Path, keep_last_n: int
) -> Path | None:
    """Run ``sqlite3.Connection.backup`` → ``backup_dir``.

    Files are named ``monitor-YYYYMMDD-HHMM.db``. After writing, the
    newest ``keep_last_n`` are retained; older ones are unlinked. Any
    I/O exception is logged and swallowed so the caller (a background
    loop) survives a transient disk-full.

    Returns the absolute path of the newly-written backup on success,
    or None when the source DB is missing / unusable.
    """
    if not src.is_file():
        log.debug("backup: source %s does not exist yet; skipping", src)
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    dst = backup_dir / f"monitor-{ts}.db"
    try:
        with sqlite3.connect(str(src)) as src_conn, sqlite3.connect(
            str(dst)
        ) as dst_conn:
            src_conn.backup(dst_conn)
    except Exception as exc:  # noqa: BLE001
        log.error("backup: sqlite3 .backup(%s → %s) failed: %s", src, dst, exc)
        try:
            dst.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return None
    # Retention: keep the newest ``keep_last_n`` monitor-*.db files.
    try:
        files = sorted(
            backup_dir.glob("monitor-*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep_last_n:]:
            try:
                stale.unlink()
            except Exception as exc:  # noqa: BLE001
                log.debug("backup: failed to prune %s: %s", stale, exc)
    except Exception:  # noqa: BLE001
        log.exception("backup: retention sweep failed")
    log.info("backup: wrote %s", dst)
    return dst


async def _backup_loop() -> None:
    """Run ``_perform_sqlite_backup`` every ``backup_interval_h`` hours.

    Mirrors the ``watchdog_loop`` shape so lifespan cleanup is uniform.
    A zero interval disables the loop entirely — useful when a DB on
    a separate host / backend is managed out-of-band.
    """
    settings = get_settings()
    interval_h = int(settings.backup_interval_h)
    if interval_h <= 0:
        log.info("backup loop disabled (ARGUS_BACKUP_INTERVAL_H=0)")
        return
    src = _sqlite_path_from_url(settings.db_url)
    if src is None:
        log.info(
            "backup loop disabled (db_url %s is not a file-backed sqlite)",
            settings.db_url,
        )
        return

    backup_dir = BACKEND_DIR / "data" / "backups"
    interval_s = interval_h * 3600
    # Wait one full interval before the first run so short-lived test
    # suites that briefly spin up the app don't spam backup files.
    while True:
        try:
            await asyncio.sleep(interval_s)
            await asyncio.to_thread(
                _perform_sqlite_backup,
                src,
                backup_dir,
                settings.backup_keep_last_n,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("backup loop iteration failed")


async def _retention_loop() -> None:
    """Run ``retention.sweep_once`` every N minutes.

    Config knob: ``ARGUS_RETENTION_SWEEP_MINUTES`` (default 60). A value
    of 0 disables the loop entirely — use that when an external cron
    drives the admin ``/api/admin/retention/sweep`` endpoint instead.

    With multiple uvicorn workers, ``_try_singleton_lock("retention")`` in
    lifespan() ensures only one worker runs this loop; the rest skip it
    to avoid racing each other's DELETEs.
    """
    settings = get_settings()
    interval_m = int(settings.retention_sweep_interval_minutes)
    if interval_m <= 0:
        log.info("retention loop disabled (ARGUS_RETENTION_SWEEP_MINUTES=0)")
        return
    # Import lazily so tests that stub out the sweeper don't have to
    # monkeypatch app.py as well.
    from backend.retention import sweep_once

    interval_s = interval_m * 60
    while True:
        try:
            await asyncio.sleep(interval_s)
            async with SessionLocal() as db:
                stats = await sweep_once(db, settings)
                await db.commit()
                total = sum(v for v in stats.values() if v > 0)
                if total:
                    log.info("retention: swept %d rows (%s)", total, stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("retention loop iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create tables and notifier state on startup; dispose on shutdown."""
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Schema management: operators are expected to run ``alembic upgrade head``
    # manually before starting the server (see ``backend/README.md``). We do
    # NOT call ``alembic`` in-process here — Phase 3 rounds 2/3 proved that
    # dispatching ``asyncio.run(run_migrations_online())`` from an already
    # running event loop is a warning-ridden no-op that used to silently fall
    # back to ``init_db()``. Keep a single clear contract: alembic is
    # operator-driven, ``init_db()`` only covers the "fresh install / tests"
    # path via ``metadata.create_all`` for any missing tables.
    await init_db()

    # Seed the built-in demo project so the UI always has something
    # polished to render (even on a brand-new deployment before any
    # user has reported their first batch). Idempotent: a second
    # startup on a warm DB is effectively free because seed_demo
    # short-circuits when ProjectMeta row exists. Test fixtures set
    # ``ARGUS_SKIP_DEMO_SEED=1`` so existing counter / visibility
    # tests stay clean; a dedicated test suite toggles it off to
    # exercise the seeder path explicitly.
    if os.environ.get("ARGUS_SKIP_DEMO_SEED", "").strip() not in (
        "1", "true", "yes",
    ):
        try:
            async with SessionLocal() as demo_session:
                await seed_demo(demo_session)
        except Exception as exc:  # noqa: BLE001
            # A failure to seed must never block startup — we still
            # want the API to come up so an operator can inspect logs.
            log.warning("demo seeder failed: %s", exc, exc_info=True)

    if os.environ.get("ARGUS_SKIP_EMAIL_SEED", "").strip() not in ("1", "true", "yes"):
        try:
            from backend.services.email_templates import (
                seed_default_templates,
            )
            async with SessionLocal() as _etpl_session:
                await seed_default_templates(_etpl_session)
                await _etpl_session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("email template seeder failed: %s", exc, exc_info=True)

    # Advisory: encrypted system_config rows tied to ARGUS_JWT_SECRET
    # become unreadable if the JWT secret is rotated without first
    # setting ARGUS_CONFIG_KEY. Logged once at startup.
    try:
        from backend.services.secrets import warn_if_using_jwt_fallback
        await warn_if_using_jwt_fallback()
    except Exception as exc:  # noqa: BLE001
        log.debug("config-key fallback probe failed: %s", exc)

    _configure_notifications(app)

    # Background task: periodically purge expired JWT blacklist entries.
    # Intentionally NOT singleton-guarded: the blacklist is per-process
    # in-memory state, so every worker must purge its own copy.
    try:
        purge_task = start_blacklist_purge_task()
    except RuntimeError:
        purge_task = None  # already-running event loop edge cases

    # Hydrate the JWT rotation cache from the DB so the first request
    # after boot doesn't pay the cold-cache hit. Tolerant of a missing
    # ``system_config`` table (the test fixtures drop+create on every
    # test, and ``init_db`` above just created it).
    try:
        async with SessionLocal() as _jwt_session:
            await _jwt_rotation_hydrate_cache(_jwt_session, force=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("jwt rotation cache hydrate failed (non-fatal): %s", exc)

    # Background task: periodically clear ``previous_secret`` after the
    # 24h grace window elapses. Singleton-locked because the row is
    # cluster-wide; one worker per host clearing it is enough.
    rotation_sweep_task: Optional[asyncio.Task] = None
    if _try_singleton_lock("jwt_rotation"):
        try:
            rotation_sweep_task = start_rotation_sweep_task()
        except RuntimeError:
            rotation_sweep_task = None

    # Cross-worker singleton loops (Team Scale). When ARGUS_WORKERS>1 we
    # must NOT let every uvicorn process run the DB-wide sweepers in
    # parallel — they'd race each other's DELETEs and multiply backup
    # files. Each loop gates on an fcntl advisory lock so only the first
    # worker to boot runs it.
    watchdog_task: Optional[asyncio.Task] = None
    backup_task: Optional[asyncio.Task] = None
    retention_task: Optional[asyncio.Task] = None

    # Background task: watchdog rule engine (in-app notification bell).
    # Disabled when ARGUS_WATCHDOG_ENABLED=false (default: enabled).
    _watchdog_enabled = os.environ.get(
        "ARGUS_WATCHDOG_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no")
    if _watchdog_enabled and _try_singleton_lock("watchdog"):
        watchdog_task = asyncio.create_task(watchdog_loop())

    # Background task: SQLite backup cron (Team A / roadmap #34).
    # Disabled in tests via ARGUS_BACKUP_INTERVAL_H=0.
    if _try_singleton_lock("backup"):
        try:
            backup_task = asyncio.create_task(_backup_loop())
        except RuntimeError:
            backup_task = None

    # Background task: retention sweeper (Team Scale). Wraps sweep_once in
    # a periodic loop. The admin endpoint still exists for manual kicks.
    if _try_singleton_lock("retention"):
        try:
            retention_task = asyncio.create_task(_retention_loop())
        except RuntimeError:
            retention_task = None

    # Background task: async email worker (Team Email BE-2). One-per-cluster
    # via the same singleton lock primitive so multi-worker deployments
    # don't fan out duplicate sends. Toggle off via
    # ``ARGUS_EMAIL_WORKER_ENABLED=false`` for tests / API-only mode.
    email_worker_task: Optional[asyncio.Task] = None
    _email_worker_enabled = os.environ.get(
        "ARGUS_EMAIL_WORKER_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no")
    if _email_worker_enabled and _try_singleton_lock("email_worker"):
        try:
            from backend.services.email_worker import start_worker
            email_worker_task = start_worker()
        except Exception as exc:  # noqa: BLE001
            log.warning("email worker failed to start: %s", exc, exc_info=True)
            email_worker_task = None

    try:
        yield
    finally:
        if purge_task is not None:
            purge_task.cancel()
            try:
                await asyncio.wait_for(purge_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("purge task shutdown: %s", exc)
        if watchdog_task is not None:
            watchdog_task.cancel()
            try:
                await asyncio.wait_for(watchdog_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("watchdog task shutdown: %s", exc)
        if backup_task is not None:
            backup_task.cancel()
            try:
                await asyncio.wait_for(backup_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("backup task shutdown: %s", exc)
        if retention_task is not None:
            retention_task.cancel()
            try:
                await asyncio.wait_for(retention_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("retention task shutdown: %s", exc)
        if email_worker_task is not None:
            try:
                from backend.services.email_worker import stop_worker
                await stop_worker(timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                log.debug("email worker shutdown: %s", exc)
        if rotation_sweep_task is not None:
            rotation_sweep_task.cancel()
            try:
                await asyncio.wait_for(rotation_sweep_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("jwt rotation sweep shutdown: %s", exc)
        await dispose_db()


def create_app() -> FastAPI:
    """Build and return a fully wired FastAPI application."""
    settings = get_settings()
    _install_access_log_redaction()
    app = FastAPI(
        title="Argus",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(settings.base_url),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    app.include_router(auth_api.router)
    app.include_router(oauth_api.router)
    app.include_router(tokens_api.router)
    app.include_router(events_api.router)
    app.include_router(events_stream_api.router)
    app.include_router(sse_multiplex_api.router)
    app.include_router(batches_api.router)
    # Executor agent endpoints (#103 v0.1.5 slice). Hosts running
    # ``argus-agent`` register here, then poll for rerun/stop work
    # items issued by the Executor service.
    app.include_router(agents_api.router)
    # Artifact upload + download (POST /api/jobs/{id}/artifacts +
    # GET/DELETE /api/artifacts/{id}). The /api/jobs sub-router MUST
    # register BEFORE ``jobs_api.router`` because FastAPI matches routes
    # in registration order — ``jobs_api.router`` owns the broader
    # ``/{batch_id}/{job_id}`` pattern which would otherwise swallow
    # ``/{job_id}/artifacts`` (treating ``"artifacts"`` as the job id).
    app.include_router(artifacts_api.jobs_router)
    app.include_router(artifacts_api.artifacts_router)
    app.include_router(jobs_api.router)
    app.include_router(resources_api.router)
    app.include_router(hosts_api.router)
    # BACKEND-C: sharing + public links + admin
    app.include_router(shares_api.batch_share_router)
    app.include_router(shares_api.project_share_router)
    app.include_router(public_api.owner_public_router)
    # Public-projects routes MUST register BEFORE public_router so the
    # deeper ``/api/public/projects/...`` paths win over the generic
    # ``/api/public/{slug}`` catch-all.
    app.include_router(public_api.public_projects_router)
    app.include_router(public_api.public_router)
    app.include_router(admin_api.router)
    app.include_router(admin_config_api.router)
    app.include_router(admin_security_api.router)
    app.include_router(email_admin_api.router)
    app.include_router(notifications_email_api.me_router)
    app.include_router(notifications_email_api.unsubscribe_router)
    # /api/me/notification_prefs + /api/me/resend_verification (#108).
    # Sits next to ``notifications_email_api.me_router`` because it
    # shares the ``/api/me`` prefix; FastAPI is happy with two routers
    # under the same prefix as long as the inner paths don't collide.
    app.include_router(me_api.router)
    # Per-batch email subscription overrides (owner-only).  Registered
    # AFTER ``batches_api.router`` so its ``/{batch_id}/...`` routes
    # don't shadow this one — but FastAPI matches the more specific
    # path first regardless of order, so the relative position is
    # informational only.
    app.include_router(batch_email_subscription_api.router)
    # Per-project multi-recipient notification list (v0.1.4).  The
    # public ``/api/unsubscribe/recipient/{token}`` is a sibling
    # router so middleware that gates ``/api/projects/*`` doesn't
    # accidentally require auth on the unsubscribe path.
    app.include_router(project_notification_recipients_api.router)
    app.include_router(project_notification_recipients_api.unsubscribe_router)
    # BACKEND-E: Dashboard IA + stars/pins/compare
    app.include_router(dashboard_api.router)
    app.include_router(projects_api.router)
    app.include_router(stars_api.router)
    app.include_router(pins_api.router)
    app.include_router(compare_api.router)
    # Per-user UI preferences (demo visibility, locale, ...)
    app.include_router(preferences_api.router)
    # Watchdog notification bell
    app.include_router(notifications_api.router)
    # Observability: per-user GPU-hours tile
    app.include_router(stats_api.router)
    # Optuna studies — multirun visualisation (v0.2 hyperopt-ui).
    # Sits next to ``compare`` because it consumes the same trial-grouping
    # primitives (``Job.extra``); RBAC + soft-delete flow through the
    # shared :class:`VisibilityResolver`.
    app.include_router(studies_api.router)
    # Meta: empty-state hints catalog (#30)
    app.include_router(meta_api.router)

    # Mount the built frontend at root if available. This is optional — during
    # development the Vite dev server runs on :5173 and proxies /api through.
    if FRONTEND_DIST_PATH.is_dir():
        # SPA fallback: Vue Router HTML5 history mode needs deep paths
        # (`/projects/foo`, `/batches/bar`) to return index.html on hard
        # refresh. StaticFiles(html=True) only serves index.html for bare
        # directories, not arbitrary paths, so we add a catch-all route
        # BEFORE the mount that serves index.html for any non-API path that
        # didn't match an earlier router.
        from fastapi import HTTPException
        from fastapi.responses import FileResponse

        _INDEX_HTML = FRONTEND_DIST_PATH / "index.html"
        _API_PREFIXES = (
            "api/", "docs", "redoc", "openapi.json", "schemas/",
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            # Pass-through for api/docs/openapi — let them 404 rather than
            # get swallowed by the SPA shell. (/health has its own route
            # registered earlier so it never reaches this handler.)
            if full_path.startswith(_API_PREFIXES):
                raise HTTPException(status_code=404)
            candidate = FRONTEND_DIST_PATH / full_path
            if candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(_INDEX_HTML))

        app.mount(
            "/",
            StaticFiles(directory=str(FRONTEND_DIST_PATH), html=True),
            name="frontend",
        )
        log.info("serving frontend from %s (SPA fallback enabled)", FRONTEND_DIST_PATH)
    else:
        log.info(
            "frontend dist %s not found; API-only mode", FRONTEND_DIST_PATH
        )

    return app


app = create_app()
