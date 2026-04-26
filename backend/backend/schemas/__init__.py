"""Pydantic DTO package.

Re-exports every symbol from :mod:`backend.schemas.events` so existing
``from backend.schemas import EventIn`` imports keep working even though this
is now a package rather than a flat module. Auth DTOs live in
:mod:`backend.schemas.auth`; token DTOs in :mod:`backend.schemas.tokens`.
"""

from backend.schemas.events import (  # noqa: F401
    PAYLOAD_MODELS,
    BatchDoneData,
    BatchEventsIn,
    BatchEventsOut,
    BatchEventResult,
    BatchFailedData,
    BatchOut,
    BatchStartData,
    EnvSnapshotData,
    EpochPoint,
    EventAccepted,
    EventIn,
    EventOut,
    EventType,
    JobDoneData,
    JobEpochData,
    JobFailedData,
    JobOut,
    JobStartData,
    LogLineData,
    ResourceSnapshotData,
    ResourceSnapshotOut,
    Source,
)
from backend.schemas.tokens import (  # noqa: F401
    TokenCreateIn,
    TokenCreateOut,
    TokenOut,
)

__all__ = [
    "PAYLOAD_MODELS",
    "BatchDoneData",
    "BatchEventsIn",
    "BatchEventsOut",
    "BatchEventResult",
    "BatchFailedData",
    "BatchOut",
    "BatchStartData",
    "EnvSnapshotData",
    "EpochPoint",
    "EventAccepted",
    "EventIn",
    "EventOut",
    "EventType",
    "JobDoneData",
    "JobEpochData",
    "JobFailedData",
    "JobOut",
    "JobStartData",
    "LogLineData",
    "ResourceSnapshotData",
    "ResourceSnapshotOut",
    "Source",
    "TokenCreateIn",
    "TokenCreateOut",
    "TokenOut",
]
