"""Pydantic DTOs for per-host resource timeseries endpoint.

These schemas back ``GET /api/hosts/{host}/timeseries``, which returns
bucketed resource usage stacked by ``batch_id`` so the frontend can
render a stacked-area chart showing how the host's GPU/RAM is split
among concurrent batches over time.

Note: ``batch_id`` values come from ``ResourceSnapshot.batch_id``
(added in migration 008, PR-A). Code uses ``getattr(row, "batch_id",
None)`` for forward-compatibility while PR-A is in flight.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class HostTimeseriesBucket(BaseModel):
    """One time-bucket in the host resource timeseries.

    ``ts``       — bucket start as ISO 8601 string (UTC, Z-suffixed).
    ``total``    — host-level value for the metric in this bucket (may
                   equal the sum of per-batch values when only per-process
                   data is available, or come directly from the
                   host-level column when present).
    ``by_batch`` — per-batch_id breakdown; may be empty when all
                   snapshots in this bucket lack ``batch_id`` or
                   ``proc_*`` process-level columns (both are PR-A
                   additions).
    """

    model_config = ConfigDict(extra="forbid")

    ts: str
    total: float | None
    by_batch: dict[str, float]


class HostTimeseriesOut(BaseModel):
    """Response body for ``GET /api/hosts/{host}/timeseries``."""

    model_config = ConfigDict(extra="forbid")

    host: str
    metric: str
    buckets: list[HostTimeseriesBucket]
    host_total_capacity: float | None
