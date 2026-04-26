"""Event schema helpers mirroring `schemas/event_v1.json` (v1.1).

Lightweight builder + client-side shallow validation. Full JSON-Schema
validation lives in the backend and in the test suite.

v1.1 changes (vs v1.0):
  * Adds `event_id` (UUID, required). The client auto-generates one per
    event in `build_event`. The backend uses it to deduplicate retried
    POSTs — spill replay, network-flap retries, etc. all become safe.
  * Bumps `schema_version` to "1.1".
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("argus")

SCHEMA_VERSION = "1.1"

EVENT_TYPES = frozenset({
    "batch_start", "batch_done", "batch_failed",
    "job_start", "job_epoch", "job_done", "job_failed",
    "resource_snapshot", "log_line",
})

# event_types that MUST have a non-null job_id
JOB_SCOPED = frozenset({"job_start", "job_epoch", "job_done", "job_failed", "log_line"})

# Loose UUID regex: 8-4-4-4-12 hex. Accepts any UUID version (we always
# generate v4 ourselves but tolerate others if the caller supplies one).
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def utc_now_iso() -> str:
    """ISO 8601 UTC with trailing Z, e.g. 2026-04-23T09:23:06Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_event_id() -> str:
    """Fresh UUID4 string for the `event_id` field."""
    return str(uuid.uuid4())


def is_valid_uuid(value: Any) -> bool:
    """True if `value` is a string shaped like a UUID."""
    return isinstance(value, str) and bool(_UUID_RE.match(value))


@dataclass
class EventSource:
    project: str
    host: Optional[str] = None
    user: Optional[str] = None
    commit: Optional[str] = None
    command: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        # schema forbids additionalProperties in source; drop Nones
        return {k: v for k, v in asdict(self).items() if v is not None}


def drop_none(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def build_event(
    event_type: str,
    batch_id: str,
    source: EventSource,
    data: Dict[str, Any],
    job_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    event_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble a full event dict matching event_v1.json (v1.1).

    `event_id` auto-generates a UUID4 if omitted. Callers can override
    it for testing or to replay a specific event with a stable id.
    """
    return {
        "event_id": event_id or new_event_id(),
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "timestamp": timestamp or utc_now_iso(),
        "batch_id": batch_id,
        "job_id": job_id,
        "source": source.to_dict(),
        "data": data or {},
    }


def validate_event(event: Dict[str, Any]) -> bool:
    """Cheap structural check. Returns True/False, never raises.

    Catches common bugs (missing batch_id/event_id, unknown event_type,
    wrong schema_version, malformed UUID) before we enqueue. Full
    JSON-Schema validation is delegated to the backend and tests.
    """
    try:
        if not isinstance(event, dict):
            return False
        if event.get("schema_version") != SCHEMA_VERSION:
            logger.warning(
                "event dropped: schema_version mismatch (%r, expected %r)",
                event.get("schema_version"), SCHEMA_VERSION,
            )
            return False
        if not is_valid_uuid(event.get("event_id")):
            logger.warning(
                "event dropped: event_id missing or not a UUID (%r)",
                event.get("event_id"),
            )
            return False
        et = event.get("event_type")
        if et not in EVENT_TYPES:
            logger.warning("event dropped: unknown event_type %r", et)
            return False
        batch_id = event.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            logger.warning("event dropped: missing/empty batch_id")
            return False
        source = event.get("source")
        if not isinstance(source, dict) or not source.get("project"):
            logger.warning("event dropped: source.project missing")
            return False
        if et in JOB_SCOPED and not event.get("job_id"):
            logger.warning("event dropped: %s requires job_id", et)
            return False
        return True
    except Exception:  # pragma: no cover
        logger.exception("validate_event crashed; dropping")
        return False
