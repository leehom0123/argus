"""JWT issuing, verification, refresh and a simple in-memory blacklist.

We stick with HS256 (symmetric secret) because this service is a single
process; moving to a multi-replica deployment later would swap to RS256 +
keyrotation but not change callers.

The blacklist is a dict ``{token_hash: expire_epoch}``. ``logout`` stores
the hash so refresh + retries can't reuse the token until its natural
expiry. A background coroutine cleans entries older than ``exp`` every 60s.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as _jwt

from backend.config import get_settings
from backend.services import jwt_rotation as _rotation

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------


class _TokenBlacklist:
    """In-memory token hash → expiry epoch map.

    For the MVP single-process deployment this is fine. A multi-process
    setup would plug Redis / memcached in behind the same interface.
    """

    def __init__(self) -> None:
        self._entries: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _hash(token: str) -> str:
        # We store *hashes* not raw tokens so a dump of the process image
        # can't be replayed.
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def add(self, token: str, expires_at: float) -> None:
        async with self._lock:
            self._entries[self._hash(token)] = expires_at

    async def contains(self, token: str) -> bool:
        async with self._lock:
            h = self._hash(token)
            exp = self._entries.get(h)
            if exp is None:
                return False
            if exp < time.time():
                # Lazy-expire to keep the hot path cheap.
                self._entries.pop(h, None)
                return False
            return True

    async def purge(self) -> int:
        now = time.time()
        async with self._lock:
            dead = [k for k, v in self._entries.items() if v < now]
            for k in dead:
                self._entries.pop(k, None)
            return len(dead)

    def clear(self) -> None:
        self._entries.clear()


_BLACKLIST = _TokenBlacklist()


async def blacklist_token(token: str, exp_epoch: float) -> None:
    await _BLACKLIST.add(token, exp_epoch)


async def is_blacklisted(token: str) -> bool:
    return await _BLACKLIST.contains(token)


async def _purge_loop(interval: float = 60.0) -> None:
    """Background task — runs inside FastAPI's lifespan."""
    while True:
        try:
            await asyncio.sleep(interval)
            removed = await _BLACKLIST.purge()
            if removed:
                log.debug("jwt blacklist: purged %d expired entries", removed)
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("jwt blacklist purge failed: %s", exc)


def start_blacklist_purge_task() -> asyncio.Task:
    """Spawn the periodic purge. Call once in app lifespan startup."""
    return asyncio.create_task(_purge_loop(), name="jwt-blacklist-purge")


def clear_blacklist_for_tests() -> None:
    """Test helper — reset between tests so state doesn't leak."""
    _BLACKLIST.clear()


# ---------------------------------------------------------------------------
# Encode / decode
# ---------------------------------------------------------------------------


class JWTError(Exception):
    """Anything that prevents a token from being accepted."""


def create_access_token(
    user_id: int,
    *,
    extra_claims: dict[str, Any] | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, int, str]:
    """Sign and return ``(token, exp_epoch_seconds, jti)``.

    The ``jti`` claim is a random URL-safe id so two tokens issued in the
    same wall-clock second can't collide in the blacklist. Callers that
    want to track the issued JWT in the ``active_sessions`` table (for the
    Settings > Sessions panel) should pass the returned ``jti`` to
    :func:`record_active_session` after committing the token response.
    """
    settings = get_settings()
    ttl = ttl_seconds if ttl_seconds is not None else settings.jwt_ttl_seconds
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=ttl)
    jti = secrets.token_urlsafe(12)
    payload: dict[str, Any] = {
        "user_id": int(user_id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "iss": settings.jwt_issuer,
        "jti": jti,
    }
    if extra_claims:
        payload.update(extra_claims)
    # Sign with the rotation-aware secret. Falls back to
    # ``settings.jwt_secret`` (the env-var path) when the DB has no
    # ``current_secret`` set, so pre-rotation deployments are byte-identical.
    signing_secret = _rotation.get_signing_secret()
    token = _jwt.encode(
        payload, signing_secret, algorithm=settings.jwt_algorithm
    )
    # pyjwt >= 2 returns ``str``. Be defensive for older versions.
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token, int(exp.timestamp()), jti


async def record_active_session(
    db,  # type: AsyncSession (avoid import cycle with backend.models)
    *,
    jti: str,
    user_id: int,
    issued_at_epoch: int,
    expires_at_epoch: int,
    user_agent: str | None,
    ip: str | None,
) -> None:
    """Insert an :class:`ActiveSession` row for a freshly-issued JWT.

    Imported lazily so :mod:`backend.auth.jwt` stays free of ORM deps for
    the unit tests that don't touch the DB. The caller is responsible for
    calling ``db.commit()`` if they want the row persisted immediately;
    we only flush.
    """
    # Lazy import: avoids circular dep (models imports db → db imports
    # config → config doesn't touch jwt, but keep it simple).
    from backend.models import ActiveSession

    def _iso(epoch: int) -> str:
        return (
            datetime.fromtimestamp(epoch, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    # Cap user_agent at 512 bytes to prevent 10KB+ header DoS; QA flagged
    # the absence of a bound as a medium-severity hardening gap.
    if user_agent is not None and len(user_agent) > 512:
        user_agent = user_agent[:512]
    row = ActiveSession(
        jti=jti,
        user_id=int(user_id),
        issued_at=_iso(issued_at_epoch),
        expires_at=_iso(expires_at_epoch),
        user_agent=user_agent,
        ip=ip,
        last_seen_at=_iso(issued_at_epoch),
        revoked_at=None,
    )
    db.add(row)
    await db.flush()


async def touch_session_last_seen(db, jti: str) -> None:
    """Bump ``last_seen_at`` for an active session. Best-effort."""
    from sqlalchemy import update as _update

    from backend.models import ActiveSession

    now = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    try:
        await db.execute(
            _update(ActiveSession)
            .where(ActiveSession.jti == jti)
            .values(last_seen_at=now)
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("session last_seen bump failed (non-fatal): %s", exc)


async def is_session_revoked(db, jti: str) -> bool:
    """Return True iff ``active_sessions.revoked_at`` is non-null for ``jti``.

    Unknown ``jti`` → False (fall through to other auth checks). A real
    revocation from the Settings panel sets ``revoked_at`` AND adds the
    token to the in-memory blacklist, so either signal stops the request.
    """
    from sqlalchemy import select as _select

    from backend.models import ActiveSession

    try:
        row = (
            await db.execute(
                _select(ActiveSession.revoked_at).where(
                    ActiveSession.jti == jti
                )
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        log.debug("session revoke lookup failed (fail-open): %s", exc)
        return False
    return bool(row)


def decode_token(token: str, *, check_blacklist_sync: bool = False) -> dict[str, Any]:
    """Decode and validate ``token``.

    Raises :class:`JWTError` for any reason a caller should reject the
    token: bad signature, wrong issuer, expired, malformed. We do *not* look
    at the blacklist here because that's async; callers should use
    :func:`is_blacklisted` separately in an ``async`` context.

    Dual-key flow (v0.2 #109): when the admin has rotated the JWT secret
    via ``POST /api/admin/security/jwt/rotate`` we accept both
    ``current_secret`` (always tried first) and ``previous_secret``
    (within the 24h grace window) so already-issued tokens keep working
    without forcing every user to re-login. The legacy
    ``ARGUS_JWT_SECRET`` env var stays in the candidate list as the
    initial-bootstrap fallback.

    Expiry / issuer / malformed-token errors override "bad signature on
    every candidate" — those failure modes are independent of the
    secret used. We surface them eagerly so a long-expired token
    doesn't lazily masquerade as an "invalid signature" message.
    """
    settings = get_settings()
    # Best-effort: refresh the rotation cache from the DB if we haven't
    # touched it in a while. No-op when called from inside a running
    # event loop (deps layer already hydrates per request).
    try:
        _rotation.ensure_cache_fresh_sync()
    except Exception as exc:  # noqa: BLE001
        log.debug("decode_token: ensure_cache_fresh_sync ignored: %r", exc)

    candidates = _rotation.get_verification_secrets()
    last_error: Exception | None = None
    for secret in candidates:
        try:
            payload = _jwt.decode(
                token,
                secret,
                algorithms=[settings.jwt_algorithm],
                options={"require": ["exp", "iat", "user_id", "iss"]},
                issuer=settings.jwt_issuer,
            )
            return payload
        except _jwt.ExpiredSignatureError as exc:
            # Expiry doesn't depend on the key — bail out early.
            raise JWTError("token expired") from exc
        except _jwt.InvalidIssuerError as exc:
            # Wrong issuer is also key-independent.
            raise JWTError("invalid issuer") from exc
        except _jwt.InvalidSignatureError as exc:
            # Try the next candidate.
            last_error = exc
            continue
        except _jwt.InvalidTokenError as exc:
            # Malformed / missing-claim style: surface immediately.
            raise JWTError(f"invalid token: {exc}") from exc
    # Exhausted every secret without a successful verification.
    detail = f"invalid token: {last_error}" if last_error else "invalid token"
    raise JWTError(detail)


def refresh_access_token(token: str) -> tuple[str, int, str]:
    """Validate the current token and issue a fresh one for the same user.

    Any extra claims beyond the standard set are preserved so future code
    that puts scopes / tenant ids into the token keeps working. Returns
    ``(new_token, new_exp_epoch, new_jti)``.
    """
    payload = decode_token(token)
    reserved = {"iat", "exp", "iss", "jti"}
    extra = {k: v for k, v in payload.items() if k not in reserved and k != "user_id"}
    return create_access_token(int(payload["user_id"]), extra_claims=extra)
