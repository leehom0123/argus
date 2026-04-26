"""Argon2id wrappers for password hashing.

We use the ``argon2-cffi`` defaults (time_cost=3, memory_cost=65536,
parallelism=4) which aim at ~200ms per hash on a modern laptop — strong
enough to matter, fast enough to not cripple login. ``PasswordHasher``
supports automatic rehashing when the parameters change, which is useful for
future migrations.
"""
from __future__ import annotations

import logging

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

log = logging.getLogger(__name__)

# One hasher per process. Thread-safe.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an argon2id-encoded hash of ``password``.

    The output includes the algorithm + parameters + salt + digest, so it's
    self-describing and safe to store as a single TEXT column.
    """
    if not isinstance(password, str) or len(password) == 0:
        raise ValueError("password must be a non-empty string")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check ``password`` against ``password_hash``. Returns bool, never raises.

    ``argon2-cffi`` raises ``VerifyMismatchError`` on mismatch. We flatten
    that into a boolean so callers can use `if verify_password(...)` without
    a try/except.
    """
    if not password or not password_hash:
        return False
    try:
        _hasher.verify(password_hash, password)
        return True
    except VerifyMismatchError:
        return False
    except InvalidHashError:
        log.warning("password_hash is not a valid argon2 encoding")
        return False
    except Exception as exc:  # noqa: BLE001
        # Defence in depth: never let a verification bug leak as auth success.
        log.error("unexpected error verifying password: %s", exc)
        return False


def needs_rehash(password_hash: str) -> bool:
    """True if the hash was produced with outdated parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
