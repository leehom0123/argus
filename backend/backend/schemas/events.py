"""Pydantic v2 models mirroring schemas/event_v1.json.

The event schema uses a discriminated union over ``event_type``. The public
entry point is :class:`EventIn`; internal handlers can switch on
``event.event_type`` or pattern-match ``event.data`` on concrete payload type.

Unknown/extra fields in ``data`` are accepted for forward compatibility, but
the top-level envelope is strict (``additionalProperties=false`` in the JSON
Schema).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Shared sub-schemas
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """``source`` block from the event envelope."""

    model_config = ConfigDict(extra="forbid")

    project: str
    host: str | None = None
    user: str | None = None
    commit: str | None = None
    command: str | None = None


# ---------------------------------------------------------------------------
# Per-event-type payload models.
#
# ``data`` payload is intentionally lenient (extra fields allowed) so the
# reporter client can include vendor-specific metadata without breaking the
# contract.
# ---------------------------------------------------------------------------


class BatchStartData(BaseModel):
    model_config = ConfigDict(extra="allow")

    experiment_type: str | None = None
    n_total_jobs: int | None = None
    command: str | None = None


class BatchDoneData(BaseModel):
    model_config = ConfigDict(extra="allow")

    n_done: int | None = None
    n_failed: int | None = None
    total_elapsed_s: float | None = None


class BatchFailedData(BaseModel):
    model_config = ConfigDict(extra="allow")

    error: str | None = None
    n_done: int | None = None
    n_failed: int | None = None


class JobStartData(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    dataset: str | None = None
    config_digest: str | None = None
    run_dir: str | None = None


class JobEpochData(BaseModel):
    model_config = ConfigDict(extra="allow")

    epoch: int
    train_loss: float | None = None
    val_loss: float | None = None
    lr: float | None = None


class JobDoneData(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    elapsed_s: float | None = None
    train_epochs: int | None = None
    metrics: dict[str, Any] | None = None
    resources: dict[str, Any] | None = None


class JobFailedData(BaseModel):
    model_config = ConfigDict(extra="allow")

    error: str | None = None
    elapsed_s: float | None = None


class ResourceSnapshotData(BaseModel):
    model_config = ConfigDict(extra="allow")

    gpu_util_pct: float | None = None
    gpu_mem_mb: float | None = None
    gpu_mem_total_mb: float | None = None
    gpu_temp_c: float | None = None
    cpu_util_pct: float | None = None
    ram_mb: float | None = None
    ram_total_mb: float | None = None
    disk_free_mb: float | None = None
    disk_total_mb: float | None = None


class LogLineData(BaseModel):
    model_config = ConfigDict(extra="allow")

    level: str | None = None
    message: str | None = None


class EnvSnapshotData(BaseModel):
    """One-time reproducibility snapshot emitted at ``on_train_begin``.

    Every field is optional — the reporter may omit anything it can't
    collect on a given host (e.g. ``git_sha`` is None in a non-git
    workspace). ``extra="allow"`` lets reporter versions add new keys
    without a coordinated backend release.
    """

    model_config = ConfigDict(extra="allow")

    git_sha: str | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    python_version: str | None = None
    pip_freeze: list[str] | None = None
    hydra_config_digest: str | None = None
    hydra_config_content: str | None = None
    hostname: str | None = None


# ---------------------------------------------------------------------------
# Event envelope.
#
# We deliberately keep this as a single Pydantic class with ``data: dict``
# rather than a discriminated union of nine concrete event classes. JSON
# Schema draft-07 doesn't encode the discriminator pattern cleanly, and the
# per-type validation we actually care about happens inside the ingest
# handler where we call ``<Specific>Data.model_validate(raw)``.
# ---------------------------------------------------------------------------


EventType = Literal[
    "batch_start",
    "batch_done",
    "batch_failed",
    "job_start",
    "job_epoch",
    "job_done",
    "job_failed",
    "resource_snapshot",
    "log_line",
    "env_snapshot",
]


# Registry used by the ingest handler to validate the ``data`` payload
# against the right schema for a given ``event_type``. Keeping it as a dict
# lookup (rather than a tagged union) means unknown future event types round-
# trip safely as long as schema_version still says "1.0".
PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "batch_start": BatchStartData,
    "batch_done": BatchDoneData,
    "batch_failed": BatchFailedData,
    "job_start": JobStartData,
    "job_epoch": JobEpochData,
    "job_done": JobDoneData,
    "job_failed": JobFailedData,
    "resource_snapshot": ResourceSnapshotData,
    "log_line": LogLineData,
    "env_snapshot": EnvSnapshotData,
}


class EventIn(BaseModel):
    """Incoming event envelope validated at the FastAPI boundary.

    Schema evolution (see design §6):
      * ``"1.0"`` — original contract, no ``event_id`` field
      * ``"1.1"`` — adds **required** client-generated ``event_id`` (UUID)
        so the backend can deduplicate spill replay safely

    During the transition window both versions are accepted. v1.0 events
    cannot be deduped — they always insert a new row. v1.1 events route
    through the idempotency lookup.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        ..., description="Either '1.0' (legacy) or '1.1' (current)."
    )
    event_type: EventType
    timestamp: str
    batch_id: str = Field(..., min_length=1, max_length=128)
    job_id: Optional[str] = Field(default=None, max_length=256)
    source: Source
    data: dict[str, Any] = Field(default_factory=dict)
    # Optional on the wire: v1.0 clients don't send it, v1.1 clients must.
    # The per-event-type handler enforces v1.1 presence at request time so
    # the error message can include the schema version context.
    event_id: Optional[str] = Field(
        default=None,
        min_length=8,
        max_length=64,
        description=(
            "Client-generated UUID for idempotency (required on v1.1)."
        ),
    )

    @field_validator("event_id")
    @classmethod
    def _strip_empty_event_id(cls, v: str | None) -> str | None:
        # An empty string coming over the wire is effectively "absent" —
        # tolerate it so clients mid-migration don't hit 422 for
        # "".
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class EventAccepted(BaseModel):
    """Response body for POST /api/events."""

    accepted: bool = True
    event_id: int  # database id of the persisted event row
    deduplicated: bool = Field(
        default=False,
        description=(
            "True if the client-supplied event_id matched an existing "
            "row and this request was a no-op re-send."
        ),
    )


class BatchEventResult(BaseModel):
    """Per-event outcome inside the batch ingest response."""

    model_config = ConfigDict(extra="forbid")

    event_id: str | None = None  # client UUID if supplied
    status: Literal["accepted", "rejected", "deduplicated"]
    db_id: int | None = None
    error: str | None = None


class BatchEventsIn(BaseModel):
    """Body for ``POST /api/events/batch``."""

    model_config = ConfigDict(extra="forbid")

    events: list[EventIn] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="1..500 events to ingest in one round-trip.",
    )


class BatchEventsOut(BaseModel):
    """Response for ``POST /api/events/batch``."""

    accepted: int
    rejected: int
    results: list[BatchEventResult]


# ---------------------------------------------------------------------------
# Read-side response models.
# ---------------------------------------------------------------------------


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    experiment_type: str | None = None
    project: str
    user: str | None = None
    host: str | None = None
    command: str | None = None
    n_total: int | None = None
    n_done: int = 0
    n_failed: int = 0
    status: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    # Reproducibility snapshot decoded from Batch.env_snapshot_json (migration 014)
    env_snapshot: dict | None = None
    # "Rerun with overrides" lineage (migration 012)
    source_batch_id: str | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    batch_id: str
    model: str | None = None
    dataset: str | None = None
    status: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    elapsed_s: int | None = None
    metrics: dict[str, Any] | None = None
    # Guardrails flag set by the idle-job detector (#13).
    is_idle_flagged: bool = False
    # Roadmap #21 — FLOPS / throughput hover-card data. Flat fields on
    # top of the free-form ``metrics`` dict so frontends don't need to
    # know reporter key names. Missing inputs map to ``None``.
    avg_batch_time_ms: float | None = None
    gpu_memory_peak_mb: float | None = None
    n_params: int | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_id: str
    job_id: str | None = None
    event_type: str
    timestamp: str
    schema_version: str
    data: dict[str, Any] | None = None


class ResourceSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    host: str
    timestamp: str
    gpu_util_pct: float | None = None
    gpu_mem_mb: float | None = None
    gpu_mem_total_mb: float | None = None
    gpu_temp_c: float | None = None
    cpu_util_pct: float | None = None
    ram_mb: float | None = None
    ram_total_mb: float | None = None
    disk_free_mb: float | None = None
    disk_total_mb: float | None = None


class EpochPoint(BaseModel):
    """One row of job epoch timeseries."""

    model_config = ConfigDict(extra="allow")

    timestamp: str
    epoch: int
    train_loss: float | None = None
    val_loss: float | None = None
    lr: float | None = None
