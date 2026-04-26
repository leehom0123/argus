"""Pydantic DTOs for ``GET /api/batches/compact``.

The compact endpoint bundles everything a :class:`BatchCompactCard`
needs — batch meta, jobs, latest job_epoch per job, and the most recent
resource snapshots — into one request so the ``/batches`` page doesn't
fan out to ``1 + N×4`` calls. See ``batches.py::list_batches_compact``
for the assembly logic.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from backend.schemas.events import (
    BatchOut,
    JobOut,
    ResourceSnapshotOut,
)


class JobEpochLatestItem(BaseModel):
    """Latest ``job_epoch`` row for one job.

    Matches the per-job shape emitted by the existing
    ``GET /api/batches/{id}/epochs/latest`` so frontend code already
    consuming that endpoint can reuse the same type.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    epoch: int
    train_loss: float | None = None
    val_loss: float | None = None
    lr: float | None = None
    val_loss_trace: list[float] = []


class BatchCompactItem(BaseModel):
    """Everything a BatchCompactCard needs for one batch."""

    model_config = ConfigDict(extra="forbid")

    batch: BatchOut
    jobs: list[JobOut]
    epochs_latest: list[JobEpochLatestItem]
    resources: list[ResourceSnapshotOut]


class BatchCompactListOut(BaseModel):
    """Response envelope for ``GET /api/batches/compact``."""

    model_config = ConfigDict(extra="forbid")

    batches: list[BatchCompactItem]
