"""Pydantic DTOs for ``/api/studies`` — Optuna trial visualization (#v0.2-hyperopt-ui).

A "study" is the logical grouping of jobs that ran under the same
Optuna ``study_name`` (Hydra multirun + ``hydra/sweeper: optuna``).
Sibyl's ``Monitor`` callback emits ``optuna.{study_name, trial_id, params}``
on the ``job_start`` event; the events router stashes the dict on
``Job.extra`` (everything except ``model``/``dataset`` lands there).

Reads are visibility-filtered through :class:`VisibilityResolver` so a
caller never sees trials from batches they cannot read.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class StudySummary(BaseModel):
    """One row of ``GET /api/studies``.

    ``best_value`` follows the study's ``direction`` if the reporter
    emits one; otherwise we fall back to MIN over the canonical
    ``MSE`` metric (the platform's de-facto loss column) so the FE
    can sort sanely. ``last_run`` is ISO-8601 of the most recent
    trial's ``start_time``.
    """

    model_config = ConfigDict(from_attributes=False)

    study_name: str
    n_trials: int
    n_done: int
    n_failed: int
    best_value: float | None = None
    best_metric: str | None = None
    direction: str | None = None
    sampler: str | None = None
    last_run: str | None = None


class StudyListOut(BaseModel):
    studies: list[StudySummary]


class TrialRow(BaseModel):
    """One row of ``GET /api/studies/{name}``.

    ``params`` is the flat hyperparameter dict Sibyl emitted on
    ``job_start`` (e.g. ``{"optimizer.lr": 1e-3, "model.d_model": 128}``).
    ``value`` is the trial's headline metric — populated from
    ``job.metrics`` keyed by ``best_metric`` if known, else ``MSE``.
    """

    model_config = ConfigDict(from_attributes=False)

    trial_id: int
    job_id: str
    batch_id: str
    status: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    elapsed_s: int | None = None
    params: dict[str, Any] = {}
    value: float | None = None
    metric_name: str | None = None
    metrics: dict[str, Any] | None = None


class StudyDetailOut(BaseModel):
    """Body of ``GET /api/studies/{name}``."""

    study_name: str
    direction: str | None = None
    sampler: str | None = None
    n_trials: int
    n_done: int
    n_failed: int
    best_value: float | None = None
    best_metric: str | None = None
    param_keys: list[str]
    metric_keys: list[str]
    trials: list[TrialRow]


class TrialDetailOut(BaseModel):
    """Body of ``GET /api/studies/{name}/trials/{id}``."""

    study_name: str
    trial_id: int
    job_id: str
    batch_id: str
    status: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    elapsed_s: int | None = None
    params: dict[str, Any] = {}
    metrics: dict[str, Any] | None = None
    value: float | None = None
    metric_name: str | None = None
