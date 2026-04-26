"""Centralised runtime-configuration reader (DB > env > default).

Every call site that used to read ``os.getenv("ARGUS_FOO")`` or
``settings.foo`` directly is encouraged to migrate to
:func:`get_config(group, key, default=None)` so admins can edit the
value live from the Settings → Admin UI without a redeploy.

Read precedence
---------------
1. ``system_config`` row for ``(group, key)``.  Decrypted on the
   fly when ``encrypted=True``.
2. ``os.environ[<env-var-name>]`` per the :data:`ENV_MAP` mapping.
3. The caller-supplied ``default``.

Adding a new tunable
--------------------
* Pick a ``(group, key)`` pair (groups today: ``oauth``, ``smtp``,
  ``retention``, ``feature_flags``, ``demo``).
* Add it to :data:`ENV_MAP` if it has a corresponding env var.
* Add it to :data:`SECRET_KEYS` if storage MUST be encrypted (so
  the API auto-flips ``encrypted=True`` on write).

All values are JSON-encoded inside ``value_json`` so the helper
returns ``int``/``bool``/``list``/``dict``/``str`` natively.  Env
vars come back as raw strings — callers that want a typed result
must coerce themselves OR rely on the fact that the Settings UI
writes a typed JSON value when the admin saves the form.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import SystemConfig
from backend.services.secrets import decrypt, encrypt

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

# (group, key) → ARGUS_* env var name.  Lets us fall back to the
# existing 12-factor config when no DB override is set.
ENV_MAP: dict[tuple[str, str], str] = {
    # OAuth — GitHub provider
    ("oauth", "github_enabled"):       "ARGUS_GITHUB_OAUTH_ENABLED",
    ("oauth", "github_client_id"):     "ARGUS_GITHUB_CLIENT_ID",
    ("oauth", "github_client_secret"): "ARGUS_GITHUB_CLIENT_SECRET",
    # SMTP — outbound email
    ("smtp", "host"):       "ARGUS_SMTP_HOST",
    ("smtp", "port"):       "ARGUS_SMTP_PORT",
    ("smtp", "user"):       "ARGUS_SMTP_USER",
    ("smtp", "password"):   "ARGUS_SMTP_PASS",
    ("smtp", "from"):       "ARGUS_SMTP_FROM",
    ("smtp", "use_tls"):    "ARGUS_SMTP_USE_TLS",
    # Retention — rolling DB sweep caps (days)
    ("retention", "snapshot_days"):      "ARGUS_RETENTION_SNAPSHOT_DAYS",
    ("retention", "log_line_days"):      "ARGUS_RETENTION_LOG_LINE_DAYS",
    ("retention", "job_epoch_days"):     "ARGUS_RETENTION_JOB_EPOCH_DAYS",
    ("retention", "event_other_days"):   "ARGUS_RETENTION_EVENT_OTHER_DAYS",
    ("retention", "demo_data_days"):     "ARGUS_RETENTION_DEMO_DATA_DAYS",
}

# (group, key) pairs whose values MUST be stored encrypted.  The
# admin-config PUT route auto-flips ``encrypted=True`` for these so a
# careless caller can't accidentally land a plaintext secret.
SECRET_KEYS: set[tuple[str, str]] = {
    ("oauth", "github_client_secret"),
    ("smtp", "password"),
}

# Display-only metadata so the admin UI can render hints + hide the
# raw env name.  Optional; missing entries get an empty description.
DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("oauth", "github_enabled"):
        "Enable the GitHub OAuth login button.",
    ("oauth", "github_client_id"):
        "OAuth app client id from github.com/settings/developers.",
    ("oauth", "github_client_secret"):
        "OAuth app client secret. Stored encrypted.",
    ("oauth", "github_callback"):
        "Callback URL registered with GitHub. Defaults to "
        "<base_url>/api/auth/oauth/github/callback.",
    ("smtp", "host"):
        "SMTP server hostname.",
    ("smtp", "port"):
        "SMTP port (587 for STARTTLS, 465 for SMTPS).",
    ("smtp", "user"):
        "SMTP login username.",
    ("smtp", "password"):
        "SMTP login password. Stored encrypted.",
    ("smtp", "from"):
        "From-address used by outbound notifications.",
    ("smtp", "use_tls"):
        "Upgrade plain SMTP to STARTTLS on connect.",
    ("retention", "snapshot_days"):
        "Resource snapshots older than this are deleted.",
    ("retention", "log_line_days"):
        "Log-line events older than this are deleted.",
    ("retention", "job_epoch_days"):
        "Job-epoch events older than this are deleted.",
    ("retention", "event_other_days"):
        "Other event rows older than this are deleted.",
    ("retention", "demo_data_days"):
        "Demo-host snapshots older than this are deleted.",
}


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def _coerce_env(raw: str) -> Any:
    """Best-effort JSON-decode an env-var string.

    Lets ``ARGUS_RETENTION_BATCH_DAYS=30`` parse as int 30, the same
    shape the admin UI writes.  If JSON parsing fails the raw string
    is returned (preserving existing behaviour for paths / hosts).
    """
    if raw is None:
        return None
    raw_stripped = raw.strip()
    if not raw_stripped:
        return raw
    try:
        return json.loads(raw_stripped)
    except (json.JSONDecodeError, ValueError):
        # Booleans live in env as "true" / "false" / "1" / "0";
        # JSON only handles the lowercase forms, so cover the rest.
        lowered = raw_stripped.lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        return raw


async def get_config(
    db: AsyncSession,
    group: str,
    key: str,
    default: Any | None = None,
) -> Any:
    """Return the live value for ``(group, key)``.

    Order: DB row (decrypted if marked encrypted) → matching env var
    (best-effort JSON-coerced) → ``default``.  Tolerant of missing
    rows / corrupt JSON / bad ciphertext — every failure mode logs a
    warning and continues the fallback chain rather than 500ing the
    caller.
    """
    row = await db.get(SystemConfig, (group, key))
    if row is not None:
        try:
            stored = row.value_json
            if row.encrypted:
                stored_plain = decrypt(stored)
                # Decrypted secrets are always strings.
                return stored_plain
            return json.loads(stored)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "runtime_config.get_config(%s, %s): row unreadable (%r); "
                "falling back to env/default",
                group, key, exc,
            )
            # fall through to env / default

    env_name = ENV_MAP.get((group, key))
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val is not None and env_val != "":
            return _coerce_env(env_val)

    return default


# ---------------------------------------------------------------------------
# Write helpers (used by the admin-config router + tests)
# ---------------------------------------------------------------------------


def _serialise(value: Any) -> str:
    """JSON-encode ``value`` using the same convention as feature_flag."""
    return json.dumps(value, default=str, sort_keys=True)


async def set_config(
    db: AsyncSession,
    *,
    group: str,
    key: str,
    value: Any,
    encrypted: bool | None = None,
    description: str | None = None,
    updated_by: int | None = None,
) -> SystemConfig:
    """Upsert a (group, key) → value row.

    ``encrypted`` defaults to ``True`` for keys in
    :data:`SECRET_KEYS`, ``False`` otherwise.  Encrypted values are
    coerced to ``str`` first (Fernet only accepts bytes).  The caller
    owns the ``await db.commit()`` so this can participate in a
    larger transaction.
    """
    if encrypted is None:
        encrypted = (group, key) in SECRET_KEYS

    if encrypted:
        # Always store encrypted secrets as a JSON string of the
        # ciphertext — that keeps ``value_json`` valid JSON across
        # the table.
        if value is None:
            value = ""
        token = encrypt(str(value))
        payload = json.dumps(token)
    else:
        payload = _serialise(value)

    row = await db.get(SystemConfig, (group, key))
    now = _utcnow_iso()
    if row is None:
        row = SystemConfig(
            group=group,
            key=key,
            value_json=payload,
            encrypted=encrypted,
            description=description or DESCRIPTIONS.get((group, key)),
            updated_by=updated_by,
            updated_at=now,
        )
        db.add(row)
    else:
        row.value_json = payload
        row.encrypted = encrypted
        if description is not None:
            row.description = description
        row.updated_by = updated_by
        row.updated_at = now
    return row


async def delete_config(
    db: AsyncSession, group: str, key: str
) -> bool:
    """Delete the row if present.  Returns True if a row was removed."""
    row = await db.get(SystemConfig, (group, key))
    if row is None:
        return False
    await db.delete(row)
    return True


def env_value_for(group: str, key: str) -> str | None:
    """Public accessor used by the admin API to surface ``source: env``."""
    env_name = ENV_MAP.get((group, key))
    if env_name is None:
        return None
    raw = os.environ.get(env_name)
    return raw if raw not in (None, "") else None


__all__ = [
    "DESCRIPTIONS",
    "ENV_MAP",
    "SECRET_KEYS",
    "delete_config",
    "env_value_for",
    "get_config",
    "set_config",
]
