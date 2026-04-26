"""Unit tests for per-job ETA computation (services.eta.compute_job_eta).

Three cases:
  1. Happy path   — ≥2 epoch events, valid timestamps → non-null eta_s
  2. Warming up   — <2 epoch events → eta_s is None
  3. Finished     — epochs_done >= epochs_total → eta_s is 0
"""
from __future__ import annotations

import pytest

from backend.services.eta import compute_job_eta, JobEtaResult


# ---------------------------------------------------------------------------
# Case 1: happy path
# ---------------------------------------------------------------------------


def test_happy_path_eta_computed():
    """Two epoch events 10s apart, 10 epochs total, 8 remaining → ~80s."""
    result = compute_job_eta(
        job_id="j1",
        job_start_iso="2026-04-24T09:00:00Z",
        train_epochs_config=10,
        epoch_timestamps=[
            "2026-04-24T09:01:00Z",  # epoch 1
            "2026-04-24T09:01:10Z",  # epoch 2 — 10s gap
        ],
        now_iso="2026-04-24T09:01:10Z",
    )
    assert isinstance(result, JobEtaResult)
    assert result.job_id == "j1"
    assert result.epochs_done == 2
    assert result.epochs_total == 10
    assert result.avg_epoch_time_s is not None
    assert abs(result.avg_epoch_time_s - 10.0) < 0.5, result.avg_epoch_time_s
    # 8 remaining × 10s each
    assert result.eta_s == 80
    assert result.eta_iso is not None
    assert result.elapsed_s >= 0


# ---------------------------------------------------------------------------
# Case 2: warming up (< 2 epoch events)
# ---------------------------------------------------------------------------


def test_warming_up_returns_null_eta():
    """Only one epoch event received → not enough data → eta_s is None."""
    result = compute_job_eta(
        job_id="j2",
        job_start_iso="2026-04-24T09:00:00Z",
        train_epochs_config=50,
        epoch_timestamps=["2026-04-24T09:00:30Z"],
        now_iso="2026-04-24T09:00:35Z",
    )
    assert result.eta_s is None
    assert result.eta_iso is None
    assert result.avg_epoch_time_s is None
    assert result.epochs_done == 1


def test_no_epochs_returns_null_eta():
    """Zero epoch events → eta_s is None."""
    result = compute_job_eta(
        job_id="j3",
        job_start_iso="2026-04-24T09:00:00Z",
        train_epochs_config=50,
        epoch_timestamps=[],
        now_iso="2026-04-24T09:00:05Z",
    )
    assert result.eta_s is None
    assert result.epochs_done == 0


# ---------------------------------------------------------------------------
# Case 3: already finished (epochs_done >= epochs_total)
# ---------------------------------------------------------------------------


def test_finished_job_eta_is_zero():
    """When epochs_done equals epochs_total, no remaining work → eta_s=0."""
    result = compute_job_eta(
        job_id="j4",
        job_start_iso="2026-04-24T09:00:00Z",
        train_epochs_config=5,
        epoch_timestamps=[
            "2026-04-24T09:01:00Z",
            "2026-04-24T09:02:00Z",
            "2026-04-24T09:03:00Z",
            "2026-04-24T09:04:00Z",
            "2026-04-24T09:05:00Z",
        ],
        now_iso="2026-04-24T09:05:05Z",
    )
    assert result.epochs_done == 5
    assert result.epochs_total == 5
    assert result.eta_s == 0
    assert result.avg_epoch_time_s is not None


# ---------------------------------------------------------------------------
# Fallback: missing train_epochs_config uses default 50
# ---------------------------------------------------------------------------


def test_missing_train_epochs_uses_fallback():
    """None train_epochs_config → falls back to 50, logs a warning."""
    result = compute_job_eta(
        job_id="j5",
        job_start_iso="2026-04-24T09:00:00Z",
        train_epochs_config=None,
        epoch_timestamps=[
            "2026-04-24T09:00:30Z",
            "2026-04-24T09:01:00Z",
        ],
        now_iso="2026-04-24T09:01:00Z",
    )
    assert result.epochs_total == 50
    # 48 remaining × 30s each = 1440
    assert result.eta_s is not None
    assert result.eta_s > 0
