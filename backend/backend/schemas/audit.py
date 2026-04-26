"""Pydantic DTOs for the audit log endpoints."""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

log = logging.getLogger(__name__)


class AuditLogOut(BaseModel):
    """One row out of ``audit_log``, JSON-decoded metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None = None
    action: str
    target_type: str | None = None
    target_id: str | None = None
    metadata: dict[str, Any] | None = None
    timestamp: str
    ip_address: str | None = None

    @field_validator("metadata", mode="before")
    @classmethod
    def _decode_metadata(cls, v: Any) -> Any:
        """Accept either raw JSON text (from ORM) or already-decoded dict."""
        if v is None or isinstance(v, dict):
            return v
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", errors="replace")
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError as exc:
                log.warning("audit metadata not valid JSON: %s", exc)
                return None
        return None
