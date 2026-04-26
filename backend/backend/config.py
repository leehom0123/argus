"""Runtime settings loaded from environment variables.

Settings are read lazily via ``get_settings()`` — it caches a single
``Settings`` instance per process, so tests can monkeypatch env vars before
the first call.

All env variable names use the ``ARGUS_`` prefix to avoid collisions with
other services sharing the same host. Defaults favour developer ergonomics:
if SMTP is unconfigured, ``EmailService`` falls back to stdout printing; if
``ARGUS_JWT_SECRET`` is missing we still boot with a dev-only secret but
emit a big warning so production operators don't ship it by accident.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent
DATA_DIR = BACKEND_DIR / "data"

# Sentinel used when the operator has not set ARGUS_JWT_SECRET. Short enough
# to fail the production >=32 char check; long enough to not crash dev startup.
_DEV_JWT_SECRET = "dev-secret-do-not-use-in-production"


class Settings(BaseSettings):
    """Read once from env. Safe to cache per-process."""

    model_config = SettingsConfigDict(
        env_prefix="ARGUS_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    # --- core ----------------------------------------------------------
    jwt_secret: str = Field(default=_DEV_JWT_SECRET)
    jwt_algorithm: str = Field(default="HS256")
    jwt_issuer: str = Field(default="argus")
    jwt_ttl_seconds: int = Field(default=24 * 3600)

    db_url: str = Field(
        default_factory=lambda: (
            f"sqlite+aiosqlite:///{DATA_DIR / 'argus.db'}"
        )
    )
    base_url: str = Field(default="http://localhost:5173")
    log_level: str = Field(default="INFO")
    env: str = Field(default="dev")  # 'dev' | 'prod'

    # --- smtp ----------------------------------------------------------
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_pass: str | None = None
    smtp_from: str = "noreply@argus.local"
    smtp_use_tls: bool = True

    # --- auth policy ---------------------------------------------------
    # 5 failed logins lock the account for 10 minutes.
    login_max_failures: int = 5
    login_lock_minutes: int = 10
    email_verify_ttl_hours: int = 24
    password_reset_ttl_minutes: int = 15
    # Email-change verification token lives 7 days — longer than the
    # initial verify because the user has to wait for the email to land
    # in a (possibly external) inbox they don't yet own outright.
    email_change_ttl_hours: int = 24 * 7
    password_min_length: int = 10

    # --- oauth (GitHub) ------------------------------------------------
    # NOTE (v0.1.4+): the ``github_oauth_*`` Settings fields below are
    # **env-fallback only**. The runtime source of truth is the DB-backed
    # ``system_config`` table (read via
    # :func:`backend.services.runtime_config.get_config`); operators
    # configure GitHub OAuth through the Admin Settings UI without a
    # redeploy. These ``ARGUS_GITHUB_*`` env vars stay around so a fresh
    # deploy without a configured DB row still works, and so existing
    # ops playbooks keep functioning. New code paths should prefer
    # ``backend.api.oauth._get_github_oauth_state(db)`` (which folds the
    # DB-or-env lookup into a single read) instead of these properties.
    github_oauth_enabled: bool = Field(
        default=False, alias="ARGUS_GITHUB_OAUTH_ENABLED"
    )
    github_oauth_client_id: str | None = Field(
        default=None, alias="ARGUS_GITHUB_CLIENT_ID"
    )
    github_oauth_client_secret: SecretStr | None = Field(
        default=None, alias="ARGUS_GITHUB_CLIENT_SECRET"
    )

    # --- retention ------------------------------------------------------
    # Rolling age caps for time-series tables. Batches + Jobs never purge
    # (result archive). demo-host snapshots use the short demo cap. A
    # zero sweep interval disables the in-process loop (for tests or
    # external cron).
    retention_snapshot_days: int = Field(
        default=7, alias="ARGUS_RETENTION_SNAPSHOT_DAYS"
    )
    retention_log_line_days: int = Field(
        default=14, alias="ARGUS_RETENTION_LOG_LINE_DAYS"
    )
    retention_job_epoch_days: int = Field(
        default=30, alias="ARGUS_RETENTION_JOB_EPOCH_DAYS"
    )
    retention_event_other_days: int = Field(
        default=90, alias="ARGUS_RETENTION_EVENT_OTHER_DAYS"
    )
    retention_demo_data_days: int = Field(
        default=1, alias="ARGUS_RETENTION_DEMO_DATA_DAYS"
    )
    retention_sweep_interval_minutes: int = Field(
        default=60, alias="ARGUS_RETENTION_SWEEP_MINUTES"
    )

    # --- guardrails (Team A) -------------------------------------------
    # Divergence detector: fire when val_loss grows by at least
    # ``divergence_ratio`` over ``divergence_window`` consecutive epochs,
    # or the instant val_loss becomes NaN / +Inf / -Inf.  Default: doubling
    # (2.0×) over 3 epochs matches the roadmap #12 spec.
    divergence_ratio: float = Field(
        default=2.0, alias="ARGUS_DIVERGENCE_RATIO"
    )
    divergence_window: int = Field(
        default=3, alias="ARGUS_DIVERGENCE_WINDOW"
    )
    # Idle-job detector: fire when the most recent window-min minutes of
    # ``ResourceSnapshot`` rows tied to a job show mean GPU util below 5%.
    idle_job_threshold_min: int = Field(
        default=10, alias="ARGUS_IDLE_JOB_THRESHOLD_MIN"
    )
    # Stalled-batch detector: flip a running batch's status to ``'stalled'``
    # when its newest event (or resource snapshot) is older than this many
    # minutes.  Fifteen minutes covers even long epochs on slow datasets
    # while catching ctrl-C / machine-reboot / OOM-kill cases within a
    # scan window.
    stall_timeout_min: int = Field(
        default=15, alias="ARGUS_STALL_TIMEOUT_MIN"
    )
    # How often the stalled detector runs. Kept separate from the 60 s
    # watchdog loop so heartbeat checks stay reactive without doubling the
    # existing guardrail cadence — default 120 s is plenty.
    stall_check_interval_s: int = Field(
        default=120, alias="ARGUS_STALL_CHECK_INTERVAL_S"
    )
    # How often both guardrails poll. Kept short so the roadmap
    # "within-a-minute" UX goal is met without a separate scheduler.
    batch_divergence_check_interval_s: int = Field(
        default=60, alias="ARGUS_BATCH_DIVERGENCE_CHECK_INTERVAL_S"
    )
    # Anomalous-login detector: send an informational email when a
    # login succeeds from an (ip, user_agent) pair not seen in the last
    # 30 days.  Default off in tests; flip to True in prod.
    alerts_anomalous_login_enabled: bool = Field(
        default=True, alias="ARGUS_ALERTS_ANOMALOUS_LOGIN_ENABLED"
    )
    # Backup cron: runs a SQLite ``.backup`` every ``backup_interval_h``
    # hours and keeps the newest ``backup_keep_last_n`` files in
    # ``data/backups/``.  0h disables the loop (useful for tests).
    backup_interval_h: int = Field(
        default=6, alias="ARGUS_BACKUP_INTERVAL_H"
    )
    backup_keep_last_n: int = Field(
        default=7, alias="ARGUS_BACKUP_KEEP_LAST_N"
    )

    # --- db pool (Team Perf) ------------------------------------------
    # Connection-pool knobs. Defaults live in
    # :func:`backend.db._pool_defaults_for` — compact for SQLite
    # (single-writer), 20/30 for Postgres (parallel workers + SSE).
    # Unset / None → fall back to the per-dialect defaults.
    db_pool_size: int | None = Field(
        default=None, alias="ARGUS_DB_POOL_SIZE"
    )
    db_pool_max_overflow: int | None = Field(
        default=None, alias="ARGUS_DB_POOL_MAX_OVERFLOW"
    )
    db_pool_timeout: int | None = Field(
        default=None, alias="ARGUS_DB_POOL_TIMEOUT"
    )
    db_pool_recycle: int | None = Field(
        default=None, alias="ARGUS_DB_POOL_RECYCLE"
    )

    @property
    def github_oauth_configured(self) -> bool:
        """True iff GitHub OAuth is enabled AND fully configured.

        Env-fallback view; see the ``--- oauth (GitHub) ---`` comment
        above. Runtime callers should use
        :func:`backend.api.oauth._get_github_oauth_state` which honours
        the DB-backed config row first.
        """
        return (
            self.github_oauth_enabled
            and bool(self.github_oauth_client_id)
            and bool(self.github_oauth_client_secret)
            and bool(
                self.github_oauth_client_secret.get_secret_value()
                if self.github_oauth_client_secret
                else None
            )
        )

    @field_validator("jwt_secret")
    @classmethod
    def _check_secret_length(cls, v: str) -> str:
        # Only enforce in prod. Dev default is acceptable to let pytest etc.
        # boot without env plumbing. production is denoted by env=prod.
        return v

    @property
    def smtp_configured(self) -> bool:
        """True iff enough SMTP env vars are set to attempt sending email."""
        return bool(self.smtp_host and self.smtp_from)

    def require_production_ready(self) -> list[str]:
        """Return a list of reasons the current settings are not prod-safe."""
        reasons: list[str] = []
        if self.jwt_secret == _DEV_JWT_SECRET:
            reasons.append("ARGUS_JWT_SECRET is unset (using dev fallback)")
        if len(self.jwt_secret) < 32:
            reasons.append("ARGUS_JWT_SECRET must be >=32 bytes")
        if not self.smtp_configured:
            reasons.append(
                "SMTP not configured — email falls back to stdout"
            )
        return reasons


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor. Tests clear via ``get_settings.cache_clear()``."""
    s = Settings()
    if s.env == "prod":
        issues = s.require_production_ready()
        if issues:
            for reason in issues:
                log.error("production config issue: %s", reason)
            raise RuntimeError(
                "Refusing to start in prod with unsafe defaults: "
                + "; ".join(issues)
            )
    else:
        if s.jwt_secret == _DEV_JWT_SECRET:
            log.warning(
                "ARGUS_JWT_SECRET not set — using dev fallback. "
                "Set a 32+ byte random value before deploying."
            )
        if not s.smtp_configured:
            log.warning(
                "SMTP not configured (ARGUS_SMTP_HOST empty) — "
                "emails will be printed to stdout instead of sent."
            )
    return s
