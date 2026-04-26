"""Helpers for the dual-key JWT rotation flow (v0.2 #109).

Storage shape
-------------
Three rows live under ``system_config.group = 'jwt'``:

* ``current_secret``  — Fernet-encrypted active signing secret.
* ``previous_secret`` — Fernet-encrypted previous signing secret (held
  during the 24h grace window).
* ``rotated_at``      — UTC ISO timestamp of the last rotation.

All three rows are seeded by migration ``029_jwt_dual_key`` with empty
values; reads fall back to ``ARGUS_JWT_SECRET`` (the original env-var
path) until the first rotation, so existing deployments are unaffected
until an admin clicks "Rotate".

Why a thin module
-----------------
The JWT layer is sync (PyJWT is pure-CPU), but our DB session is
async. We can't ``await`` from inside :func:`backend.auth.jwt.decode_token`
without infecting every caller. Instead, we maintain a tiny in-process
**cache** of ``(current, previous, previous_expires_at)`` that:

1. Refreshes lazily on the first ``decode`` after the cache has been
   invalidated.
2. Is invalidated explicitly by :func:`rotate_secret` after a write.
3. Falls back to the env-var secret when the DB row is empty.

This keeps the hot decode path lock-free in steady state while
guaranteeing that a rotation takes effect on the next request.

The cache is **process-local**. In a multi-worker uvicorn deployment
each worker re-reads the DB on its first decode after rotation; the
TTL is short enough that drift between workers is at most ``CACHE_TTL``
seconds (default 30s). For single-worker MVP setups this is exact.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets as _secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.runtime_config import set_config
from backend.services.secrets import InvalidToken, decrypt

log = logging.getLogger(__name__)


class RotationCooldown(Exception):
    """Raised when a rotation is attempted within ``ROTATE_COOLDOWN_SECONDS``.

    Carries ``retry_after`` (whole seconds remaining) so the API layer
    can echo it as both the JSON body and the standard ``Retry-After``
    response header.
    """

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rotation cooldown active; retry after {retry_after}s")
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How long the cached (current, previous, rotated_at) tuple is trusted
# before we force a re-read from the DB. Short enough that a rotation
# in worker A is visible to worker B within ``CACHE_TTL`` seconds.
CACHE_TTL_SECONDS = 30.0

# Grace window during which a freshly-superseded ``previous_secret`` is
# still accepted by the verifier. After this elapses the background
# sweeper clears the row so an attacker can't replay an ancient leaked
# secret indefinitely.
PREVIOUS_GRACE_SECONDS = 24 * 60 * 60  # 24h

# Anti-double-rotate cooldown. A second rotation within this window
# would overwrite the freshly-minted ``previous_secret`` with the
# *previous-previous* (i.e. blank, because we only keep one prior key),
# instantly invalidating every JWT that was issued before the first
# rotation. A curl loop or a double-clicked admin button is the typical
# trigger; the cooldown turns that footgun into a 429.
ROTATE_COOLDOWN_SECONDS = 60

# Minimum byte-count for a freshly-minted secret. URL-safe base64 of 32
# bytes is 43 chars — comfortably above the 32-byte floor that
# ``backend.config.ProductionWarnings`` requires.
NEW_SECRET_BYTES = 32

# Group name in ``system_config``. Constant so all readers/writers
# agree.
GROUP = "jwt"
KEY_CURRENT = "current_secret"
KEY_PREVIOUS = "previous_secret"
KEY_ROTATED_AT = "rotated_at"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class _SecretCache:
    """Process-local snapshot of the rotation state.

    Holds ``(current, previous, rotated_at_iso, fetched_epoch)``. The
    decode hot path checks ``time.time() - fetched_epoch >= CACHE_TTL``
    and refetches when stale. Writes (``rotate_secret``,
    ``clear_expired_previous``) call :meth:`invalidate` so the next
    decode picks up the new state.

    A ``threading.Lock`` guards mutation — the cache is read by the
    sync :func:`backend.auth.jwt.decode_token` from any worker thread
    and written by async callers via ``run_in_executor``-equivalent
    paths.
    """

    def __init__(self) -> None:
        self._current: str | None = None
        self._previous: str | None = None
        self._rotated_at: str | None = None
        self._fetched_epoch: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> tuple[str | None, str | None, str | None]:
        """Return ``(current, previous, rotated_at_iso)`` snapshot."""
        with self._lock:
            return self._current, self._previous, self._rotated_at

    def set(
        self,
        *,
        current: str | None,
        previous: str | None,
        rotated_at: str | None,
    ) -> None:
        with self._lock:
            self._current = current or None
            self._previous = previous or None
            self._rotated_at = rotated_at
            self._fetched_epoch = time.time()

    def is_stale(self) -> bool:
        with self._lock:
            return time.time() - self._fetched_epoch >= CACHE_TTL_SECONDS

    def invalidate(self) -> None:
        with self._lock:
            self._fetched_epoch = 0.0

    def reset_for_tests(self) -> None:
        with self._lock:
            self._current = None
            self._previous = None
            self._rotated_at = None
            self._fetched_epoch = 0.0


_CACHE = _SecretCache()


def reset_cache_for_tests() -> None:
    """Drop cached secret state. Tests that swap the DB call this."""
    _CACHE.reset_for_tests()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Accept both the ``...Z`` form we write and ``...+00:00``.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


async def _read_jwt_row(db: AsyncSession, key: str) -> tuple[str | None, bool]:
    """Return ``(decoded_value, encrypted_flag)`` or ``(None, False)``.

    Decryption failures are logged and surfaced as ``None`` so a botched
    Fernet key doesn't lock anyone out — the verifier still has the
    other secret and the env-var fallback to lean on.
    """
    from backend.models import SystemConfig  # local import: avoid cycles

    row = await db.get(SystemConfig, (GROUP, key))
    if row is None:
        return None, False
    raw = row.value_json
    if row.encrypted:
        # Stored as JSON string of ciphertext: ``"gAAA..."``.
        try:
            import json as _json  # noqa: PLC0415
            ciphertext = _json.loads(raw)
            if not isinstance(ciphertext, str) or ciphertext == "":
                return None, True
            return decrypt(ciphertext), True
        except (InvalidToken, ValueError, TypeError) as exc:
            log.warning(
                "jwt_rotation: failed to decrypt %s/%s: %r — "
                "ignoring this key",
                GROUP, key, exc,
            )
            return None, True
    # Plaintext (currently only ``rotated_at``). Stored as JSON.
    try:
        import json as _json  # noqa: PLC0415
        v = _json.loads(raw)
        if v in (None, ""):
            return None, False
        return str(v), False
    except (ValueError, TypeError):
        return None, False


async def load_secrets(db: AsyncSession) -> tuple[str | None, str | None, str | None]:
    """Read the three rotation rows and return decoded values.

    Returns ``(current_or_None, previous_or_None, rotated_at_iso_or_None)``.
    All three components are independently optional: a fresh deploy has
    every field empty and we fall back to the env-var path; mid-rotation
    only ``previous`` is empty (or expired); etc.
    """
    current, _ = await _read_jwt_row(db, KEY_CURRENT)
    previous, _ = await _read_jwt_row(db, KEY_PREVIOUS)
    rotated_at, _ = await _read_jwt_row(db, KEY_ROTATED_AT)
    return current, previous, rotated_at


# ---------------------------------------------------------------------------
# Sync surface — read by ``backend.auth.jwt``
# ---------------------------------------------------------------------------


def _env_fallback() -> str | None:
    """Return ``ARGUS_JWT_SECRET`` when set + non-dev, else ``None``.

    The dev sentinel is still returned — the JWT module always needs
    *some* secret to encode/decode against. The "is this production"
    check happens upstream in :mod:`backend.config`, where it's
    surfaced as a startup warning.
    """
    return os.environ.get("ARGUS_JWT_SECRET") or None


def get_signing_secret() -> str:
    """Return the secret to sign **new** tokens with.

    Order of precedence:

    1. ``current_secret`` from the DB cache (set by the rotate endpoint).
    2. ``ARGUS_JWT_SECRET`` env var (legacy / bootstrap path).
    3. The dev sentinel from :class:`backend.config.Settings` — the
       same value the env-var fallback returned before this module
       existed, so behaviour is unchanged for callers that never
       rotated.

    Never raises: callers expect a string. We pull the env-var path as
    the last fallback so :class:`Settings.jwt_secret` (which already
    knows how to surface a dev sentinel) keeps producing the same
    bytes a pre-rotation deployment used.
    """
    current, _, _ = _CACHE.get()
    if current:
        return current
    env = _env_fallback()
    if env:
        return env
    # Final fallback: pull from Settings, which is what the original
    # ``settings.jwt_secret`` line returned.
    from backend.config import get_settings  # local import: avoid cycles
    return get_settings().jwt_secret


def get_verification_secrets() -> list[str]:
    """Return the ordered list of secrets to **try** when verifying.

    Always at least one entry. The verifier in
    :func:`backend.auth.jwt.decode_token` walks the list in order and
    accepts the first signature that validates. Callers that previously
    passed a single ``settings.jwt_secret`` keep the same behaviour
    because the legacy env value is always present.
    """
    current, previous, _ = _CACHE.get()
    out: list[str] = []
    if current:
        out.append(current)
    env = _env_fallback()
    if env and env not in out:
        out.append(env)
    if previous and previous not in out:
        out.append(previous)
    if not out:
        # Same final fallback as ``get_signing_secret``.
        from backend.config import get_settings  # local import: avoid cycles
        out.append(get_settings().jwt_secret)
    return out


# ---------------------------------------------------------------------------
# Cache hydration — async, called by hot path via a thread-safe wrapper
# ---------------------------------------------------------------------------


async def hydrate_cache(db: AsyncSession, *, force: bool = False) -> None:
    """Re-read the three rows from ``db`` and update the cache.

    Tolerant of every failure mode: a missing table (tests that don't
    run migrations), a row with broken ciphertext, … all degrade to
    "use whatever the cache currently holds". The hot-path verifier
    has the env-var fallback so an empty cache + a working env still
    serves traffic.

    By default this is a no-op when the cache is fresh (``CACHE_TTL``
    seconds). Pass ``force=True`` after a write to invalidate
    immediately — :func:`rotate_secret` does this so the rotating
    worker sees its own change without waiting for the TTL.
    """
    if not force and not _CACHE.is_stale():
        return
    try:
        current, previous, rotated_at = await load_secrets(db)
    except Exception as exc:  # noqa: BLE001
        log.debug("jwt_rotation.hydrate_cache: %r", exc)
        return
    _CACHE.set(current=current, previous=previous, rotated_at=rotated_at)


def ensure_cache_fresh_sync() -> None:
    """Sync wrapper used from :func:`decode_token`.

    The decode path is sync; we cannot ``await`` here. We launch a
    one-shot DB session in a fresh event loop **only when the cache is
    stale AND no loop is currently running**. Inside an async context
    (FastAPI request handlers, normal flow) the cache is hydrated by
    explicit ``await hydrate_cache(db)`` calls in :mod:`backend.deps`,
    so this branch is a no-op.

    Kept defensive: if anything goes wrong we silently leave the cache
    as-is — the verifier still has the env-var fallback.
    """
    if not _CACHE.is_stale():
        return
    try:
        asyncio.get_running_loop()
        # We're inside an event loop already; the deps layer will
        # hydrate via ``await hydrate_cache(db)`` on the next request.
        # Doing it here would require nested-loop tricks that ban the
        # codebase doesn't allow.
        return
    except RuntimeError:
        # No running loop — safe to spin up our own briefly.
        pass
    try:
        from backend.db import SessionLocal  # local import: avoid cycles

        async def _runner() -> None:
            async with SessionLocal() as db:
                await hydrate_cache(db)

        asyncio.run(_runner())
    except Exception as exc:  # noqa: BLE001
        log.debug("ensure_cache_fresh_sync: %r", exc)


# ---------------------------------------------------------------------------
# Rotation + grace sweeper
# ---------------------------------------------------------------------------


def _mint_secret() -> str:
    """Return a fresh URL-safe ``NEW_SECRET_BYTES``-byte secret."""
    return _secrets.token_urlsafe(NEW_SECRET_BYTES)


async def rotate_secret(
    db: AsyncSession,
    *,
    actor_user_id: int | None = None,
) -> str:
    """Mint a new ``current_secret`` and demote the old one.

    The flow is intentionally simple:

    1. Read the existing ``current_secret`` (if any).
    2. Mint ``new = token_urlsafe(NEW_SECRET_BYTES)``.
    3. Write ``previous_secret = old_current`` (encrypted, blank if
       there was no prior secret).
    4. Write ``current_secret = new`` (encrypted).
    5. Write ``rotated_at = utcnow_iso``.
    6. Commit and invalidate the in-process cache.

    Returns the ISO timestamp written so the API can echo it back.
    Caller owns the audit-log entry.

    Raises :class:`RotationCooldown` when the previous rotation finished
    less than :data:`ROTATE_COOLDOWN_SECONDS` ago — see the constant's
    docstring for why this matters (back-to-back rotations would discard
    the only ``previous_secret`` and force-log-out everyone).
    """
    # Anti-double-rotate. If the last rotation is still inside the
    # cooldown window we refuse and let the caller surface a 429.
    rotated_at_str, _ = await _read_jwt_row(db, KEY_ROTATED_AT)
    last_rotated = _parse_iso(rotated_at_str)
    if last_rotated is not None:
        elapsed = (datetime.now(timezone.utc) - last_rotated).total_seconds()
        if 0 <= elapsed < ROTATE_COOLDOWN_SECONDS:
            retry_after = max(1, int(ROTATE_COOLDOWN_SECONDS - elapsed))
            raise RotationCooldown(retry_after=retry_after)

    old_current, _ = await _read_jwt_row(db, KEY_CURRENT)
    new_current = _mint_secret()
    now = _utcnow_iso()

    await set_config(
        db,
        group=GROUP,
        key=KEY_PREVIOUS,
        value=old_current or "",
        encrypted=True,
        updated_by=actor_user_id,
    )
    await set_config(
        db,
        group=GROUP,
        key=KEY_CURRENT,
        value=new_current,
        encrypted=True,
        updated_by=actor_user_id,
    )
    await set_config(
        db,
        group=GROUP,
        key=KEY_ROTATED_AT,
        value=now,
        encrypted=False,
        updated_by=actor_user_id,
    )
    await db.commit()

    # Hydrate from the DB we just wrote so the cache is exact (force
    # past the TTL — we just changed the values out from under it).
    await hydrate_cache(db, force=True)
    return now


async def clear_expired_previous(db: AsyncSession) -> bool:
    """Wipe ``previous_secret`` if the rotation is older than the grace.

    Returns ``True`` when a row was actually cleared. Idempotent — a
    second call within the same window is a no-op.
    """
    _, previous, rotated_at = await load_secrets(db)
    if not previous:
        return False
    parsed = _parse_iso(rotated_at)
    if parsed is None:
        # No timestamp → can't tell when grace started; clear defensively.
        cutoff_passed = True
    else:
        cutoff_passed = (
            datetime.now(timezone.utc) - parsed
        ) >= timedelta(seconds=PREVIOUS_GRACE_SECONDS)
    if not cutoff_passed:
        return False
    await set_config(
        db,
        group=GROUP,
        key=KEY_PREVIOUS,
        value="",
        encrypted=True,
    )
    await db.commit()
    await hydrate_cache(db, force=True)
    log.info("jwt_rotation: cleared expired previous_secret")
    return True


# ---------------------------------------------------------------------------
# Background sweep loop
# ---------------------------------------------------------------------------


async def _sweep_loop(interval: float = 3600.0) -> None:
    """Periodic check that drops the previous secret once it ages out.

    Spawned from :func:`backend.app.lifespan`. Default interval is 1
    hour — well below the 24h grace, so the row is cleared within an
    hour of becoming eligible. Tolerant of a missing DB during early
    boot (the worker that holds the singleton lock might race with
    ``init_db``).
    """
    log.info(
        "jwt_rotation: sweep loop started (interval=%ss, grace=%ss)",
        int(interval), PREVIOUS_GRACE_SECONDS,
    )
    while True:
        try:
            await asyncio.sleep(interval)
            from backend.db import SessionLocal  # local import: avoid cycles

            async with SessionLocal() as db:
                await clear_expired_previous(db)
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("jwt_rotation: sweep iteration failed: %s", exc)


def start_rotation_sweep_task(interval: float = 3600.0) -> asyncio.Task:
    """Spawn the periodic sweeper. Call once in app lifespan startup."""
    return asyncio.create_task(_sweep_loop(interval), name="jwt-rotation-sweep")


__all__ = [
    "CACHE_TTL_SECONDS",
    "GROUP",
    "KEY_CURRENT",
    "KEY_PREVIOUS",
    "KEY_ROTATED_AT",
    "PREVIOUS_GRACE_SECONDS",
    "ROTATE_COOLDOWN_SECONDS",
    "RotationCooldown",
    "clear_expired_previous",
    "ensure_cache_fresh_sync",
    "get_signing_secret",
    "get_verification_secrets",
    "hydrate_cache",
    "load_secrets",
    "reset_cache_for_tests",
    "rotate_secret",
    "start_rotation_sweep_task",
]
