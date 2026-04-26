"""Fire-and-forget client for Argus.

Public API:

    Reporter, JobContext              -- context-manager front door
    emit, new_batch_id, set_batch_id, sub_env, get_batch_id
                                       -- module-level escape hatches
    ExperimentReporter                 -- low-level class (still works)
    SCHEMA_VERSION                     -- event-contract version

Most users only need ``Reporter``. Open one with the project name:

    with Reporter(project="my-bench") as r:
        with r.job(model="dlinear", dataset="etth1") as j:
            for epoch in range(5):
                j.emit("job_epoch", epoch=epoch, train_loss=...)
"""
from __future__ import annotations

from .context import (
    JobContext,
    Reporter,
    emit,
    get_batch_id,
    new_batch_id,
    set_batch_id,
    sub_env,
)
from .identity import derive_batch_id
from .reporter import ExperimentReporter
from .schema import SCHEMA_VERSION

__all__ = [
    # high-level API
    "Reporter",
    "JobContext",
    "emit",
    "new_batch_id",
    "set_batch_id",
    "get_batch_id",
    "sub_env",
    # batch-identity helpers
    "derive_batch_id",
    # low-level compatibility
    "ExperimentReporter",
    "SCHEMA_VERSION",
]
from importlib.metadata import PackageNotFoundError as _PNF, version as _pkg_version
try:
    __version__ = _pkg_version("argus-reporter")
except _PNF:
    __version__ = "0.0.0+unknown"
