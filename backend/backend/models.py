"""SQLAlchemy ORM models mirroring docs/architecture.md.

All timestamps are stored as ISO 8601 strings (TEXT) because SQLite datetime
handling is inconsistent across drivers. Client code is expected to emit
``2026-04-23T09:23:06Z`` style values.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base


class Batch(Base):
    """A sweep / benchmark invocation that groups many jobs."""

    __tablename__ = "batch"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    experiment_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    project: Mapped[str] = mapped_column(Text, nullable=False)
    user: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str | None] = mapped_column(Text, nullable=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    n_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    n_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string

    # Ownership + soft-delete added in migration 003. ``owner_id`` is the
    # user who first created / stubbed the batch (inferred from the API
    # token that posted the first event). ``is_deleted`` lets us hide rows
    # from the default queries without losing the underlying event audit.
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Display name + filter tag added in migration 005. Neither is
    # populated by the reporter client today; they become editable once
    # the phase-4 PATCH /api/batches/{id} endpoint lands. The columns
    # exist now so the schema stays forward-compatible and reads
    # ( /api/dashboard, /api/projects/* ) can surface the values the
    # moment they get written.
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    tag: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ``source_batch_id`` — migration 012. Nullable FK pointing to the
    # batch this one was cloned from via "Rerun with overrides". The
    # logical reference is to :attr:`Batch.id`; SQLite's batch-mode FK
    # limitations keep it as a plain Text column (ORM-enforced).
    source_batch_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reproducibility snapshot — added in migration 014. JSON-encoded dict
    # with git_sha, git_branch, git_dirty, python_version, pip_freeze,
    # hydra_config_digest, hydra_config_content, hostname. Written once per
    # batch on the first ``env_snapshot`` event. NULL for old batches or
    # reporters that predate this feature.
    env_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_batch_owner", "owner_id"),
        Index("idx_batch_source_batch_id", "source_batch_id"),
        # Perf (migration 018): hot-path indexes for list-batches +
        # dashboard counters. ``start_time`` is the default ordering
        # on /api/batches; ``status`` + ``owner_id`` power the
        # "running" tiles.
        Index("idx_batch_start_time", "start_time"),
        Index("idx_batch_status_start", "status", "start_time"),
        Index("idx_batch_project_start", "project", "start_time"),
        Index("idx_batch_owner_status", "owner_id", "status"),
    )


class Job(Base):
    """Single run within a batch (model x dataset x seed)."""

    __tablename__ = "job"

    # Composite primary key on (batch_id, id). SQLAlchemy wants ``id`` declared
    # as part of the key alongside ``batch_id``.
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=False
    )
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    dataset: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    elapsed_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    # Guardrails (migration 016): idle-job detector flips this flag via
    # the watchdog when the job's GPU util stays < 5% for >10 minutes.
    # Advisory only — the job is NOT killed; frontend surfaces it as a
    # yellow badge so the user can decide.
    is_idle_flagged: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )
    # Soft delete (migration 021): flipped True by the per-job DELETE
    # endpoint. Every list / detail query filters on ``is_deleted=False``
    # so a deleted job disappears from the UI; the row itself stays so
    # historical event audit + per-batch counters keep their integrity.
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )

    # Perf (migration 018): the Job PK is ``(id, batch_id)`` with
    # ``id`` leading, so SQLite's auto-index does NOT serve
    # ``WHERE batch_id = ?`` — which is the single most common
    # Job predicate (list-batch-jobs, compare, eta, dashboard
    # counters, stats/gpu-hours).
    __table_args__ = (
        Index("idx_job_batch", "batch_id"),
        Index("idx_job_batch_status", "batch_id", "status"),
        # Covers /api/batches/{id}/eta which filters status='done'
        # and orders by end_time DESC; also /api/projects/*/leaderboard.
        Index("idx_job_batch_status_end", "batch_id", "status", "end_time"),
    )


class Event(Base):
    """Raw event row (audit log + epoch timeseries source)."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string
    # Client-generated UUID used for idempotency: retries of the same
    # logical event (e.g. spill replay) MUST reuse this value so the
    # backend can return the original db_id instead of inserting a second
    # row. v1.1 clients are required to send it; v1.0 events are accepted
    # with ``event_id=NULL`` during the transition window.
    event_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_event_batch_job", "batch_id", "job_id"),
        Index("idx_event_timestamp", "timestamp"),
        # Perf (migration 018): epoch timeseries + eta-all look up
        # ``(batch_id, job_id, event_type='job_epoch')`` ordered by
        # ``timestamp``. The 2-col composite above leaves event_type
        # as a scan, which dominates when epoch rows are common.
        Index(
            "idx_event_batch_job_type_ts",
            "batch_id", "job_id", "event_type", "timestamp",
        ),
        # Activity feed + notifications read ``event_type IN (...)``
        # across visible batches ordered by timestamp DESC. Avoids
        # scanning rows with NULL job_id.
        Index(
            "idx_event_batch_type_ts",
            "batch_id", "event_type", "timestamp",
        ),
        # Partial unique index: NULLs allowed (v1.0 events) but any
        # non-null event_id appears at most once across the whole table.
        Index(
            "idx_event_id",
            "event_id",
            unique=True,
            sqlite_where=text("event_id IS NOT NULL"),
        ),
    )


class ResourceSnapshot(Base):
    """Independent host resource timeseries."""

    __tablename__ = "resource_snapshot"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    host: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    gpu_util_pct: Mapped[float | None] = mapped_column(nullable=True)
    gpu_mem_mb: Mapped[float | None] = mapped_column(nullable=True)
    gpu_mem_total_mb: Mapped[float | None] = mapped_column(nullable=True)
    gpu_temp_c: Mapped[float | None] = mapped_column(nullable=True)
    cpu_util_pct: Mapped[float | None] = mapped_column(nullable=True)
    ram_mb: Mapped[float | None] = mapped_column(nullable=True)
    ram_total_mb: Mapped[float | None] = mapped_column(nullable=True)
    disk_free_mb: Mapped[float | None] = mapped_column(nullable=True)
    # Total disk capacity (MB) for the partition the run dir lives on.
    # Optional + nullable: older reporters and existing rows don't carry it.
    # When present the frontend computes used% = (total - free) / total to
    # render a real "disk fullness" bar instead of falling back to
    # free-GB pressure heuristics. Added in migration 020.
    disk_total_mb: Mapped[float | None] = mapped_column(nullable=True)
    # Per-process fields — added in migration 008.
    proc_cpu_pct: Mapped[float | None] = mapped_column(nullable=True)
    proc_ram_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proc_gpu_mem_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # batch_id ties a snapshot to the batch active at sample time — migration 008.
    batch_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    __table_args__ = (
        Index("idx_resource_host_ts", "host", "timestamp"),
        Index("idx_resource_snapshot_batch_id", "batch_id"),
        # Perf (migration 018): dashboard's "active hosts in the last
        # 5 minutes" sweep filters ``timestamp >= cutoff GROUP BY host``.
        # The composite above helps per-host lookups but does not help
        # timestamp-first scans; the standalone timestamp index does.
        Index("idx_resource_timestamp", "timestamp"),
    )


# ---------------------------------------------------------------------------
# Auth models — added in migration 002.
# ---------------------------------------------------------------------------


class User(Base):
    """A registered user.

    Password is stored as argon2id hash. Email verification is an explicit
    column rather than a soft flag so we can easily query "who is unverified
    and older than 7 days" for the GC job (per requirements §4.5).
    """

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Nullable since migration 007 — OAuth-provisioned users have no local
    # password. Local-auth users keep the argon2id hash here.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_login: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_login_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    locked_until: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_locale: Mapped[str] = mapped_column(
        String(16), default="en-US", nullable=False, server_default="en-US"
    )
    # ---- OAuth identity (migration 007) ------------------------------
    # ``github_id`` is the stable numeric GitHub user id (stringified);
    # ``github_login`` is the human-readable username (changeable on
    # GitHub's side, kept for display only). ``auth_provider`` tags the
    # account's origin: 'local' = email+password registration,
    # 'github' = provisioned via OAuth with no local password. A local
    # user who later links GitHub stays on 'local' — OAuth just becomes
    # a second sign-in method, and they keep the ability to log in with
    # their password.
    github_id: Mapped[str | None] = mapped_column(
        Text, nullable=True, unique=True
    )
    github_login: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_provider: Mapped[str] = mapped_column(
        Text, default="local", nullable=False, server_default="local"
    )
    # ---- Demo-project preference (migration 010) ---------------------
    # Deprecated since 2026-04-24: demo projects are now
    # unconditionally hidden from every authenticated user via
    # :class:`backend.services.visibility.VisibilityResolver`. The
    # column is retained so older rows (and old API clients that still
    # PATCH ``hide_demo=true``) don't break, but no read path consults
    # the value any more. Do NOT drop the column without a coordinated
    # client upgrade.
    hide_demo: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )
    # ---- Anomalous-login detector (migration 016) --------------------
    # JSON-encoded list of ``{"ip": str, "ua_hash": str, "last_seen": iso}``
    # entries. The login endpoint trims entries older than 30 days on
    # every successful login. When the current ``(ip, ua_hash)`` pair is
    # absent from the (post-trim) list we fire the anomalous-login email
    # and then append the pair so subsequent logins from the same place
    # stay silent.
    known_ips_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ---- Per-user notification defaults (migration 026) ---------------
    # JSON-encoded dict mapping the five UI-facing pref keys
    # (``notify_batch_done``, ``notify_batch_failed``, ``notify_job_failed``,
    # ``notify_diverged``, ``notify_job_idle``) to booleans. NULL means
    # "the user has never customised these defaults" — callers fall back
    # to the canonical defaults (matching the existing per-batch defaults
    # in :class:`BatchEmailSubscription`). Per-batch overrides on
    # ``batch_email_subscription`` always take precedence at dispatch time.
    notification_prefs_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )


class ApiToken(Base):
    """Personal API token for reporter clients.

    The plaintext token is returned exactly once at creation — afterwards
    the server only knows the SHA-256 ``token_hash``. ``display_hint``
    stores the first 8 chars of plaintext so the UI can disambiguate
    multiple tokens without exposing a usable secret.

    ``scope`` is one of:
      * ``reporter`` — prefix ``em_live_``; may POST /api/events* and GET own data
      * ``viewer``   — prefix ``em_view_``; read-only (for read-only share setups)
    """

    __tablename__ = "api_token"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    display_hint: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Eager-load the owning user; needed whenever the auth layer resolves
    # a token because the router immediately wants ``token.user``.
    user: Mapped["User"] = relationship("User", lazy="selectin")

    __table_args__ = (
        Index("idx_api_token_lookup", "token_hash", "revoked"),
        Index("idx_api_token_user", "user_id"),
    )


class EmailVerification(Base):
    """One-shot token for email verification *and* password reset.

    ``kind`` disambiguates the two flows; ``consumed`` flips to True once used
    so the same link cannot be replayed.
    """

    __tablename__ = "email_verification"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # 'verify' | 'reset_password' | 'email_change'
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Optional opaque payload bound to the token. Currently used only by
    # the ``email_change`` flow to store the requested new email so the
    # verify endpoint can apply it without trusting query-string input.
    # Added in migration 022.
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_email_verification_user", "user_id"),
    )


# ---------------------------------------------------------------------------
# Sharing / admin / audit — added in migration 004 (BACKEND-C).
# ---------------------------------------------------------------------------


class BatchShare(Base):
    """Grant a specific batch to a specific grantee.

    Composite PK ``(batch_id, grantee_id)`` so the same grantee can't be
    added twice to the same batch. ``permission`` is ``'viewer'`` or
    ``'editor'``; ``'editor'`` lets the grantee mutate the batch row via
    the future edit endpoints (MVP: name / tags / manual-fail / delete).
    """

    __tablename__ = "batch_share"

    batch_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("batch.id", ondelete="CASCADE"),
        primary_key=True,
    )
    grantee_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True
    )

    __table_args__ = (
        Index("idx_batch_share_grantee", "grantee_id"),
    )


class ProjectShare(Base):
    """Grant every batch in ``(owner, project)`` to a grantee.

    Composite PK ``(owner_id, project, grantee_id)``. Adding a row here
    covers all the owner's current AND future batches under that project
    name automatically — callers re-run :meth:`VisibilityResolver`
    queries on every read so the grant takes effect immediately.
    """

    __tablename__ = "project_share"

    owner_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    project: Mapped[str] = mapped_column(Text, primary_key=True)
    grantee_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_project_share_grantee", "grantee_id"),
        Index("idx_project_share_owner_project", "owner_id", "project"),
    )


class PublicShare(Base):
    """Public read-only URL for a batch.

    ``slug`` is a 20-char URL-safe random string (generated via
    :func:`secrets.token_urlsafe(15)` → 20 chars) that serves as the
    sole authentication token for anonymous ``GET /api/public/{slug}``
    requests. ``expires_at`` is optional; when set and elapsed, the
    public endpoint returns 410 Gone.
    """

    __tablename__ = "public_share"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True
    )
    expires_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    view_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    last_viewed: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_public_share_batch", "batch_id"),
    )


class AuditLog(Base):
    """Append-only ledger of auth / share / admin actions.

    ``action`` is a short kebab-or-snake string (``'login_success'``,
    ``'share_add'``, ``'feature_flag_update'``, ...). ``target_type`` +
    ``target_id`` let a future UI link to the affected entity.
    ``metadata_json`` holds a JSON-encoded dict for per-action context
    (e.g. which username was banned, what the new flag value is).
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_audit_log_timestamp", "timestamp"),
        Index("idx_audit_log_user_action", "user_id", "action"),
    )


# ---------------------------------------------------------------------------
# Stars + pins — added in migration 005 (BACKEND-E / Dashboard IA).
# ---------------------------------------------------------------------------


class UserStar(Base):
    """A user's favourite project or batch.

    Polymorphic over ``target_type`` so the same table serves both
    ``'project'`` (``target_id`` = project name) and ``'batch'``
    (``target_id`` = batch id). Composite PK keeps the toggle
    idempotent — re-inserting the same row is a no-op.
    """

    __tablename__ = "user_star"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_type: Mapped[str] = mapped_column(Text, primary_key=True)
    target_id: Mapped[str] = mapped_column(Text, primary_key=True)
    starred_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_user_star_target", "target_type", "target_id"),
    )


class UserPin(Base):
    """Batch pinned to the per-user compare-pool.

    The API layer caps active pins at 4 (compare view UX constraint);
    the DB allows any number but the compose endpoint guards. Foreign
    key cascades on batch delete so pins don't become dangling.
    """

    __tablename__ = "user_pin"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    batch_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("batch.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pinned_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_user_pin_batch", "batch_id"),
    )


class FeatureFlag(Base):
    """Admin-toggleable global feature flag.

    Values are stored as JSON-encoded text so we can hold bools, ints,
    and small dicts uniformly. The service layer deserialises on read.
    """

    __tablename__ = "feature_flag"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True
    )


# ---------------------------------------------------------------------------
# Project metadata (admin-controlled public-demo flag) — migration 009.
# ---------------------------------------------------------------------------


class ProjectMeta(Base):
    """Admin-editable per-project metadata.

    ``project`` is the free-form string used in :class:`Batch.project`;
    there's no FK to ``batch`` because that column isn't unique across
    rows. When ``is_public=True`` the public-read endpoints under
    ``/api/public/projects/<project>`` become reachable without
    authentication.
    """

    __tablename__ = "project_meta"

    project: Mapped[str] = mapped_column(Text, primary_key=True)
    is_public: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )
    public_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    # ``is_demo=True`` marks the seeded ``__demo_forecast__`` fixture
    # (migration 010). Since 2026-04-24, demo projects are visible
    # ONLY to anonymous visitors via ``/api/public/projects``;
    # authenticated users never see them regardless of admin status.
    # The visibility filter lives in
    # :class:`backend.services.visibility.VisibilityResolver`.
    is_demo: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )
    # Soft delete (migration 021). Projects aren't a first-class row in
    # the schema — they're just :attr:`Batch.project` strings — so we
    # mark a project deleted on its meta row. The DELETE endpoint also
    # cascades by flipping ``Batch.is_deleted=True`` for every batch
    # under the project so the existing visibility filter handles them.
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )

    __table_args__ = (
        Index("idx_project_meta_is_public", "is_public"),
        Index("idx_project_meta_is_demo", "is_demo"),
    )


# ---------------------------------------------------------------------------
# Host metadata + soft-delete — added in migration 021.
# ---------------------------------------------------------------------------


class HostMeta(Base):
    """Per-host meta row used to soft-delete hosts from the UI.

    Hosts aren't a first-class entity in the schema — they're inferred
    from :attr:`ResourceSnapshot.host`. This table lets an admin hide a
    retired host from the UI without dropping the underlying snapshot
    history (which other surfaces, like batch detail's resources tab,
    still depend on for older runs).
    """

    __tablename__ = "host_meta"

    host: Mapped[str] = mapped_column(Text, primary_key=True)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )
    deleted_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    hidden_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_host_meta_is_deleted", "is_deleted"),
    )


# ---------------------------------------------------------------------------
# In-app notification — added in migration 011_notifications.
# ---------------------------------------------------------------------------


class Notification(Base):
    """A watchdog alert surfaced in the in-app notification bell.

    ``rule_id`` identifies which :class:`WatchdogRule` fired.
    ``batch_id`` is nullable so future system-level alerts (not tied to a
    specific batch) can reuse the same table. ``read_at`` is NULL until the
    user acknowledges the row.
    """

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)  # info|warn|error
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_notification_user_created", "user_id", "created_at"),
        Index("idx_notification_batch", "batch_id"),
    )


# ---------------------------------------------------------------------------
# Job artifact uploads — migration 013_artifact (roadmap #8).
# ---------------------------------------------------------------------------


class Artifact(Base):
    """A file uploaded by the reporter and attached to a job.

    The file itself is persisted on the local filesystem at
    ``settings.artifact_storage_dir / storage_path``; only the metadata
    lives in the DB. ``mime`` is captured at upload time so the download
    endpoint can set the correct ``Content-Type`` without re-sniffing.
    ``label`` is a free-form string the frontend uses to group artifacts
    (e.g. ``visualizations``, ``analysis``, ``predictions``) — no
    enumeration on the DB side so callers can add new buckets without a
    schema change.
    """

    __tablename__ = "artifact"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    # No FK to job: Job uses a composite PK (id, batch_id) which is
    # awkward to reference. We enforce referential integrity at the API
    # layer by looking up ``(job_id, batch_id)`` before accepting an
    # upload. ``batch_id`` is denormalised so list / GC queries don't
    # need a join.
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Path relative to the artifact storage root so a deployment can
    # move ``artifact_storage_dir`` without rewriting every row.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("idx_artifact_job", "job_id"),
        Index("idx_artifact_batch", "batch_id"),
    )


# ---------------------------------------------------------------------------
# Active JWT sessions — migration 015 (issue #31).
# ---------------------------------------------------------------------------


class ActiveSession(Base):
    """One row per issued JWT so the Settings > Sessions panel can list + revoke.

    The row is inserted by :func:`backend.auth.jwt.create_access_token` after
    the token is signed and the caller has the user/IP/UA context to stamp.
    ``get_current_user`` bumps ``last_seen_at`` on every authenticated
    request so the UI can show a rough "last active" chip.

    Revocation flips ``revoked_at`` here *and* adds the token to the
    in-memory blacklist; the enforcement path in
    :func:`backend.deps.get_current_user` consults both (DB first so a
    restart doesn't drop revocations on the floor). We deliberately don't
    delete the row on revoke so future audit-log / admin views can render
    "you logged out device X at 14:02".
    """

    __tablename__ = "active_sessions"

    # jti is the random URL-safe id set on the JWT ``jti`` claim; we reuse
    # it here so the session and token share one identifier.
    jti: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    issued_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_active_sessions_user", "user_id"),
        Index("idx_active_sessions_expires", "expires_at"),
    )



# ---------------------------------------------------------------------------
# Email system (Team Email — migration 019).
# ---------------------------------------------------------------------------


class SmtpConfig(Base):
    """Admin-configurable SMTP connection details (single row, id=1)."""

    __tablename__ = "smtp_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )
    smtp_host: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587, nullable=False)
    smtp_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_from_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_from_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    use_tls: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("1")
    )
    use_ssl: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )


class EmailTemplate(Base):
    """Per (event_type, locale) subject + body. ``is_system`` flag."""

    __tablename__ = "email_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("idx_email_template_event_locale", "event_type", "locale", unique=True),
    )


class NotificationSubscription(Base):
    """Per-(user, project, event_type) opt-in."""

    __tablename__ = "notification_subscription"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    project: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("1")
    )
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_notif_sub_user_project_event",
            "user_id", "project", "event_type",
            unique=True,
        ),
        Index("idx_notif_sub_user", "user_id"),
    )


class EmailDeadLetter(Base):
    """Failed-send audit row (BE-2's worker retries)."""

    __tablename__ = "email_dead_letter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    to_address: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_email_dead_letter_created", "created_at"),)


class EmailUnsubscribeToken(Base):
    """One-shot unsubscribe secret."""

    __tablename__ = "email_unsubscribe_token"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    consumed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_email_unsub_user", "user_id"),)


class BatchEmailSubscription(Base):
    """Per-(user, batch) override of the project-level email subscription.

    Batch owners can fine-tune which event kinds trigger an email for
    one specific batch — e.g. mute success notifications for a long
    sweep but keep failure pings.  Composite primary key on
    ``(user_id, batch_id)`` enforces "one row per (owner, batch)".

    ``event_kinds`` is stored as a JSON-encoded list of strings drawn
    from :data:`backend.services.email_templates.SUPPORTED_EVENTS`.
    Empty list means "no events" (effectively a full mute when
    ``enabled=True``).  ``enabled=False`` is the explicit "ignore this
    override, fall back to project default" toggle the API offers via
    ``DELETE`` (the row gets deleted) but is also valid as a mute
    short-circuit when callers want to keep the row around.
    """

    __tablename__ = "batch_email_subscription"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    batch_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("batch.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    # JSON-encoded list of event_type strings.  Stored as TEXT for
    # SQLite portability; parsed by the API layer.
    event_kinds: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'[]'")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("1")
    )
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_batch_email_sub_batch", "batch_id"),
        Index("idx_batch_email_sub_user", "user_id"),
    )


class ProjectNotificationRecipient(Base):
    """Per-project email recipient (Argus user OR external address).

    A project owner (any user with at least one batch carrying the
    ``project`` name as ``Batch.owner_id == user.id``) can register an
    arbitrary mailbox to receive event notifications for the project.
    Recipients are NOT required to have a corresponding :class:`User`
    row — vendors / external collaborators / shared-team aliases are
    valid targets.

    Schema notes
    ------------
    * ``project`` is name-keyed and free-form; no FK because the
      "project" entity is just a string column on :class:`Batch`.
    * ``event_kinds`` is a JSON-encoded list of event_type strings
      drawn from :data:`backend.services.email_templates.SUPPORTED_EVENTS`.
      Stored as TEXT for SQLite portability; the API layer parses on
      the wire so the UI never sees the raw JSON.
    * ``unsubscribe_token`` is a 32-char URL-safe random secret minted
      at row creation. The public ``GET /api/unsubscribe/recipient/
      {token}`` endpoint flips ``enabled=False`` so the recipient can
      opt out from any email footer without authenticating.
    * ``UNIQUE(project, email)`` makes the "add recipient" call
      idempotent — re-adding the same email returns 409 from the API.
    """

    __tablename__ = "project_notification_recipient"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    project: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-encoded list of event_type strings. Stored as TEXT for
    # SQLite portability; parsed by the API layer.
    event_kinds: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'[]'")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("1")
    )
    added_by_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    unsubscribe_token: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_pnr_project", "project"),
        Index(
            "uq_pnr_project_email", "project", "email", unique=True
        ),
    )


class SystemConfig(Base):
    """Admin-editable runtime configuration (group, key) → JSON value.

    Replaces the env-only knobs for OAuth credentials, SMTP, retention
    caps, feature flags, and the demo-publish toggle. The
    :func:`backend.services.runtime_config.get_config` helper reads
    this table first, then falls back to the matching ``ARGUS_*`` env
    var, then to a caller-supplied default.

    Secrets (OAuth client secret, SMTP password) are stored Fernet-
    encrypted with ``encrypted=True`` — see
    :mod:`backend.services.secrets`.  The encryption key is derived
    from ``ARGUS_CONFIG_KEY`` (preferred) or ``ARGUS_JWT_SECRET``
    (fallback) so existing deployments don't need a new env var on
    upgrade.
    """

    __tablename__ = "system_config"

    group: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # JSON-encoded as TEXT for SQLite portability (the same convention
    # used by ``feature_flag.value_json`` and ``audit_log.metadata_json``).
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("0")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_system_config_group", "group"),
    )


# ---------------------------------------------------------------------------
# Executor agent (migration 029) — host-side daemon that pulls rerun/stop
# work items and spawns subprocesses.
# ---------------------------------------------------------------------------


class AgentHost(Base):
    """A registered host running ``argus-agent``.

    Created on ``POST /api/agents/register``; updated on every heartbeat /
    poll. The plaintext token is shown exactly once at registration; only
    the SHA-256 hash lives on disk (mirrors the :class:`ApiToken` pattern
    used for reporter tokens).
    """

    __tablename__ = "agent_host"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    agent_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-encoded list of capability strings (e.g. ["rerun","stop"]).
    capabilities_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(Text, nullable=True)
    registered_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Owner is the user whose JWT minted the registration. Lets us scope
    # commands to a user in tests + audit.
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True
    )

    __table_args__ = (
        Index("idx_agent_host_token_hash", "agent_token_hash", unique=True),
        Index("idx_agent_host_hostname", "hostname"),
    )


class AgentCommand(Base):
    """A pending / in-flight command issued by the Executor service.

    The Executor enqueues a row when a user clicks Rerun or Stop; the
    matching agent picks it up via ``GET /api/agents/{id}/jobs`` and acks
    via ``POST /api/agents/{id}/jobs/{cmd_id}/ack``. Status flow:
    ``pending → started → finished/failed`` (we don't track ``finished``
    yet — the reporter takes over once the subprocess is up).
    """

    __tablename__ = "agent_command"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    host_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agent_host.id"), nullable=False
    )
    # The new (rerun) or live (stop) batch this command operates on.
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    # ``rerun`` or ``stop``. Free-text on purpose so v0.1.6 can add ``pause``
    # without a migration.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON dict carrying everything the agent needs to act:
    #   rerun: {command, cwd, env, source_batch_id}
    #   stop:  {pid?}
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    ack_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_agent_command_host_status", "host_id", "status"),
        Index("idx_agent_command_batch", "batch_id"),
        # Idempotency support: dedupe re-clicks within 60s on (batch_id,
        # kind, status='pending') without a unique constraint (we want
        # multiple historical commands per batch to be allowed).
        Index("idx_agent_command_batch_kind_status", "batch_id", "kind", "status"),
    )
