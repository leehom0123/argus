"""``/api/admin/system-config`` — admin-editable runtime configuration.

Surfaces the ``system_config`` table behind a small CRUD API used by
the Settings → Admin pages.  Every route is gated on
:func:`backend.deps.require_admin`; every write is recorded in
``audit_log``.

Read shape
----------
``GET /api/admin/system-config`` returns::

    {
      "oauth": [
        {"key": "github_enabled", "value": false,
         "encrypted": false, "source": "default",
         "description": "Enable the GitHub OAuth login button.",
         "updated_at": null, "updated_by": null},
        ...
      ],
      "smtp":      [...],
      "retention": [...],
      "feature_flags": [...],
      "demo": [...]
    }

* ``source`` is one of ``"db"`` (a ``system_config`` row exists),
  ``"env"`` (an ``ARGUS_*`` env var supplies the value), or
  ``"default"``.
* ``value`` is masked to the literal string ``"***"`` for rows whose
  ``encrypted=True`` — the API never echoes plaintext secrets back to
  the browser.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.deps import get_db, require_admin
from backend.models import SystemConfig, User
from backend.services.audit import get_audit_service
from backend.services.feature_flags import DEFAULT_FLAGS, list_flags
from backend.services.runtime_config import (
    DESCRIPTIONS,
    ENV_MAP,
    SECRET_KEYS,
    delete_config,
    env_value_for,
    set_config,
)
from backend.services.secrets import SECRET_MASK, decrypt

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/system-config", tags=["admin"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SystemConfigItem(BaseModel):
    """One config entry as returned by the GET endpoints."""

    key: str
    value: Any
    encrypted: bool = False
    source: str = Field(..., description="One of 'db', 'env', 'default'")
    description: str | None = None
    updated_at: str | None = None
    updated_by: int | None = None


class SystemConfigUpdateIn(BaseModel):
    """Body for ``PUT /api/admin/system-config/{group}/{key}``."""

    model_config = ConfigDict(extra="forbid")

    value: Any = Field(..., description="JSON-serialisable new value")
    encrypted: bool | None = Field(
        default=None,
        description=(
            "Override the auto-detected encryption flag. Defaults to "
            "True for known-secret keys, False otherwise."
        ),
    )
    description: str | None = None


# ---------------------------------------------------------------------------
# Group catalogue
# ---------------------------------------------------------------------------

# Keys we always surface in the GET response, even when neither a DB
# row nor an env var supplies a value (the UI still needs an input
# field).  ``"default"`` rows in the response come from this list.
DEFAULT_KEYS: dict[str, list[tuple[str, Any]]] = {
    "oauth": [
        ("github_enabled", False),
        ("github_client_id", ""),
        ("github_client_secret", ""),
        ("github_callback", ""),
    ],
    "smtp": [
        ("host", ""),
        ("port", 587),
        ("user", ""),
        ("password", ""),
        ("from", ""),
        ("use_tls", True),
    ],
    "retention": [
        ("snapshot_days", 7),
        ("log_line_days", 14),
        ("job_epoch_days", 30),
        ("event_other_days", 90),
        ("demo_data_days", 1),
    ],
    "demo": [
        ("enabled", False),
    ],
}

KNOWN_GROUPS = set(DEFAULT_KEYS) | {"feature_flags"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _row_to_item(row: SystemConfig) -> SystemConfigItem:
    """Convert a DB row to an API item (masking secrets)."""
    if row.encrypted:
        # Don't decrypt — masked.  The unmasked path is only used by
        # ``runtime_config.get_config`` server-side.
        value: Any = SECRET_MASK
    else:
        try:
            value = json.loads(row.value_json)
        except (json.JSONDecodeError, TypeError):
            log.warning(
                "admin_config: row (%s,%s) has invalid JSON %r",
                row.group, row.key, row.value_json,
            )
            value = None
    return SystemConfigItem(
        key=row.key,
        value=value,
        encrypted=row.encrypted,
        source="db",
        description=row.description or DESCRIPTIONS.get((row.group, row.key)),
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _env_item(group: str, key: str, raw: str) -> SystemConfigItem:
    """Build a ``source: env`` item, masking secrets."""
    if (group, key) in SECRET_KEYS:
        value: Any = SECRET_MASK
    else:
        # Best-effort JSON decode so booleans/ints render correctly.
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            lowered = raw.strip().lower()
            if lowered in ("true", "false"):
                value = lowered == "true"
            else:
                value = raw
    return SystemConfigItem(
        key=key,
        value=value,
        encrypted=(group, key) in SECRET_KEYS,
        source="env",
        description=DESCRIPTIONS.get((group, key)),
    )


def _default_item(group: str, key: str, default: Any) -> SystemConfigItem:
    return SystemConfigItem(
        key=key,
        value=SECRET_MASK if (group, key) in SECRET_KEYS else default,
        encrypted=(group, key) in SECRET_KEYS,
        source="default",
        description=DESCRIPTIONS.get((group, key)),
    )


async def _build_group(
    db: AsyncSession, group: str
) -> list[SystemConfigItem]:
    """Materialise one group as a list of items in canonical order."""
    if group == "feature_flags":
        # Feature flags live in their own dedicated table; surface them
        # here so the UI has one consistent shape.  ``encrypted`` is
        # always False, ``source`` always ``"db"`` once a flag has
        # been overridden, ``"default"`` otherwise.
        merged = await list_flags(db)
        # Build a (key → row) map so we can read updated_at when present.
        from backend.models import FeatureFlag  # avoid circular at module import
        rows = (await db.execute(select(FeatureFlag))).scalars().all()
        row_map = {r.key: r for r in rows}
        items: list[SystemConfigItem] = []
        for k in sorted(merged.keys()):
            row = row_map.get(k)
            items.append(SystemConfigItem(
                key=k,
                value=merged[k],
                encrypted=False,
                source="db" if row is not None else "default",
                description=None,
                updated_at=row.updated_at if row else None,
                updated_by=row.updated_by if row else None,
            ))
        return items

    if group not in DEFAULT_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown group: {group}")

    # Pre-fetch DB rows for this group so we can do a single query.
    stmt = select(SystemConfig).where(SystemConfig.group == group)
    rows = (await db.execute(stmt)).scalars().all()
    by_key = {r.key: r for r in rows}

    items: list[SystemConfigItem] = []
    for key, default in DEFAULT_KEYS[group]:
        row = by_key.get(key)
        if row is not None:
            items.append(_row_to_item(row))
            continue
        env_raw = env_value_for(group, key)
        if env_raw is not None:
            items.append(_env_item(group, key, env_raw))
            continue
        items.append(_default_item(group, key, default))
    # Surface any extra DB rows the catalogue doesn't know about so
    # nothing gets accidentally hidden.
    catalogued = {k for k, _ in DEFAULT_KEYS[group]}
    for k, row in by_key.items():
        if k not in catalogued:
            items.append(_row_to_item(row))
    return items


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=dict[str, list[SystemConfigItem]])
async def get_all(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[SystemConfigItem]]:
    """Return every known group keyed by name."""
    out: dict[str, list[SystemConfigItem]] = {}
    for group in ["oauth", "smtp", "retention", "feature_flags", "demo"]:
        out[group] = await _build_group(db, group)
    return out


@router.get("/{group}", response_model=list[SystemConfigItem])
async def get_group(
    group: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[SystemConfigItem]:
    """Return one group as a flat list."""
    if group not in KNOWN_GROUPS:
        raise HTTPException(status_code=404, detail=f"unknown group: {group}")
    return await _build_group(db, group)


@router.put(
    "/{group}/{key}",
    response_model=SystemConfigItem,
    status_code=status.HTTP_200_OK,
)
async def put_value(
    group: str,
    key: str,
    body: SystemConfigUpdateIn,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SystemConfigItem:
    """Upsert a single config row.

    Auto-encrypts known-secret keys (see
    :data:`backend.services.runtime_config.SECRET_KEYS`).  If the body
    explicitly sets ``encrypted=True`` for a key not in the secret
    list we still honour it — admins occasionally want to encrypt a
    value that isn't on the canonical list.

    Empty-string + masked sentinel ``"***"`` short-circuit: when an
    encrypted row already exists and the caller PUTs the mask back,
    we skip the rewrite so secrets aren't mangled by an
    edit-without-re-typing form submit.
    """
    if group not in KNOWN_GROUPS:
        raise HTTPException(status_code=404, detail=f"unknown group: {group}")

    # Sentinel preservation for encrypted secrets.
    if (group, key) in SECRET_KEYS and body.value == SECRET_MASK:
        existing = await db.get(SystemConfig, (group, key))
        if existing is not None:
            return _row_to_item(existing)
        # No prior row — refuse to encrypt the literal mask. Saving
        # ``"***"`` as ciphertext is never what the operator wanted: it
        # produces a "configured" row whose plaintext is the placeholder
        # the UI shows for absent secrets, masking the real
        # mis-configuration behind a green checkmark.
        raise HTTPException(
            status_code=400,
            detail=(
                "Looks like a placeholder; submit the real secret or "
                "omit the field."
            ),
        )

    if group == "feature_flags":
        # Delegate to the feature_flags service so existing
        # consumers (`get_flag`) keep returning the right shape.
        from backend.services.feature_flags import set_flag
        await set_flag(db, key=key, value=body.value, updated_by=admin.id)
        await db.commit()
        await get_audit_service().log(
            action="system_config_set",
            user_id=admin.id,
            target_type="feature_flag",
            target_id=key,
            metadata={"group": group, "key": key},
            ip=_client_ip(request),
        )
        # Re-fetch via the merged listing so the response shape is
        # identical to the GET payload.
        items = await _build_group(db, group)
        for it in items:
            if it.key == key:
                return it
        # Unreachable — the set_flag guarantees the row exists.
        raise HTTPException(status_code=500, detail="feature flag write lost")

    row = await set_config(
        db,
        group=group,
        key=key,
        value=body.value,
        encrypted=body.encrypted,
        description=body.description,
        updated_by=admin.id,
    )
    await db.commit()

    await get_audit_service().log(
        action="system_config_set",
        user_id=admin.id,
        target_type="system_config",
        target_id=f"{group}/{key}",
        metadata={
            "group": group,
            "key": key,
            "encrypted": row.encrypted,
        },
        ip=_client_ip(request),
    )
    return _row_to_item(row)


@router.delete("/{group}/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_value(
    group: str,
    key: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove the DB row so reads fall back to env/default."""
    if group not in KNOWN_GROUPS:
        raise HTTPException(status_code=404, detail=f"unknown group: {group}")

    if group == "feature_flags":
        from backend.models import FeatureFlag
        row = await db.get(FeatureFlag, key)
        if row is None:
            raise HTTPException(status_code=404, detail="key not found")
        await db.delete(row)
        await db.commit()
    else:
        removed = await delete_config(db, group, key)
        if not removed:
            raise HTTPException(status_code=404, detail="key not found")
        await db.commit()

    await get_audit_service().log(
        action="system_config_delete",
        user_id=admin.id,
        target_type="system_config",
        target_id=f"{group}/{key}",
        metadata={"group": group, "key": key},
        ip=_client_ip(request),
    )


# ---------------------------------------------------------------------------
# Dev / debug helpers (non-secret)
# ---------------------------------------------------------------------------


__all__ = ["router", "DEFAULT_KEYS", "KNOWN_GROUPS"]


# Silence unused-import warnings for symbols re-exported above for
# downstream callers / tests.
_ = (decrypt, ENV_MAP, DEFAULT_FLAGS)
