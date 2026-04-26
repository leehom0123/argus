"""Symmetric encryption helper for ``system_config`` secrets.

Wraps :class:`cryptography.fernet.Fernet` so callers can store secrets
(OAuth client secrets, SMTP passwords, …) as ciphertext in the
``system_config`` table without leaking plaintext to backups or
``SELECT *`` debug dumps.

Key derivation
--------------
The Fernet key is 32 raw bytes, base64-encoded.  We derive it from
either:

1. ``ARGUS_CONFIG_KEY`` — operator-provided, preferred.  Accepts
   either a raw 32-byte secret (any encoding) or an already
   base64-encoded Fernet key.
2. ``ARGUS_JWT_SECRET`` — fallback, so existing deployments don't
   need a new env var the day this lands.

Either way we run the input through ``hashlib.sha256`` and base64-
url-encode the digest, which yields a syntactically valid Fernet key
deterministically.  Rotating the input rotates the key — old
ciphertext stops decrypting, which is the desired behaviour for a
"reset everything" operator action.

API
---
* :func:`encrypt(plaintext)` → ciphertext str (URL-safe base64).
* :func:`decrypt(ciphertext)` → plaintext str.  Raises
  :class:`InvalidToken` on tampering / wrong key.
* :func:`mask(value, encrypted)` → ``"***"`` when ``encrypted`` is
  True, else the value unchanged.  Used by the read API so the UI
  never sees plaintext secrets.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)


SECRET_MASK = "***"


def _derive_fernet_key(material: str) -> bytes:
    """Deterministically derive a Fernet key from arbitrary input.

    Fernet wants exactly 32 bytes, url-safe base64-encoded (44 chars
    including the trailing ``=``).  We hash the input with SHA-256 to
    normalise length, then base64-url-encode the digest.
    """
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Cache the Fernet instance for the lifetime of the process.

    ``ARGUS_CONFIG_KEY`` takes precedence; ``ARGUS_JWT_SECRET`` is the
    documented fallback so existing deployments boot without a new
    env var.  In tests the fixture sets a stable ``ARGUS_JWT_SECRET``.
    """
    material = os.environ.get("ARGUS_CONFIG_KEY") or os.environ.get(
        "ARGUS_JWT_SECRET"
    )
    if not material:
        # Last-resort dev fallback so unit tests of unrelated code
        # don't trip on a missing env var.  Production startup
        # validation in ``backend.config`` already warns when
        # ``ARGUS_JWT_SECRET`` is the dev sentinel.
        log.warning(
            "secrets._get_fernet: no ARGUS_CONFIG_KEY / ARGUS_JWT_SECRET set; "
            "using insecure dev fallback"
        )
        material = "argus-dev-config-key-do-not-use-in-prod"
    return Fernet(_derive_fernet_key(material))


def reset_for_tests() -> None:
    """Drop the cached Fernet instance.

    Tests that monkeypatch ``ARGUS_CONFIG_KEY`` / ``ARGUS_JWT_SECRET``
    must call this so the next ``encrypt`` / ``decrypt`` rebuilds the
    key from the new env values.
    """
    _get_fernet.cache_clear()


def encrypt(plaintext: str) -> str:
    """Return Fernet ciphertext for ``plaintext`` as a UTF-8 string."""
    if plaintext is None:
        plaintext = ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Return plaintext for ``ciphertext``.

    Raises :class:`cryptography.fernet.InvalidToken` if the token is
    tampered or was encrypted under a different key.  Callers are
    expected to either propagate the failure (admin UI surfaces it as
    "secret unreadable — re-enter the value") or coerce to ``""``.
    """
    return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def mask(value: str | None, encrypted: bool) -> str | None:
    """Replace encrypted values with ``"***"`` for safe display."""
    if not encrypted:
        return value
    if value is None or value == "":
        return ""
    return SECRET_MASK


async def warn_if_using_jwt_fallback() -> None:
    """Emit a startup warning when encrypted rows use the JWT fallback key.

    The Fernet key derives from ``ARGUS_CONFIG_KEY`` when set, falling
    back to ``ARGUS_JWT_SECRET`` otherwise. Operators who rotate the
    JWT secret without first setting ``ARGUS_CONFIG_KEY`` will silently
    invalidate every encrypted ``system_config`` row — old ciphertext
    no longer decrypts, and the admin UI surfaces opaque "secret
    unreadable" errors. We log a single warning at startup so the
    coupling is visible *before* a rotation event causes data loss.

    No-op when ``ARGUS_CONFIG_KEY`` is set, when no encrypted rows
    exist, or when the DB query itself fails (we never want startup
    health to depend on this advisory check).
    """
    if os.environ.get("ARGUS_CONFIG_KEY"):
        return
    try:
        from sqlalchemy import select  # noqa: PLC0415
        from backend.db import SessionLocal  # noqa: PLC0415
        from backend.models import SystemConfig  # noqa: PLC0415

        async with SessionLocal() as db:
            stmt = (
                select(SystemConfig.group)
                .where(SystemConfig.encrypted.is_(True))
                .limit(1)
            )
            row = (await db.execute(stmt)).first()
        if row is None:
            return
    except Exception as exc:  # noqa: BLE001
        log.debug("warn_if_using_jwt_fallback: probe failed: %r", exc)
        return
    log.warning(
        "ARGUS_CONFIG_KEY unset; using JWT secret fallback. "
        "Rotating JWT secret will invalidate all stored encrypted "
        "config values. Set ARGUS_CONFIG_KEY (32+ random chars) to "
        "decouple."
    )


__all__ = [
    "InvalidToken",
    "SECRET_MASK",
    "encrypt",
    "decrypt",
    "mask",
    "reset_for_tests",
    "warn_if_using_jwt_fallback",
]
