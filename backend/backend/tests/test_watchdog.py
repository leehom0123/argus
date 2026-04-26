"""Tests for backend.notifications.watchdog — one test per rule.

We use plain ``types.SimpleNamespace`` stubs rather than SQLAlchemy ORM
instances so these tests have zero DB dependency and run in milliseconds.
The predicates only access attribute names — not DB relationships — so
SimpleNamespace is a clean, non-brittle substitute.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone, timedelta

import pytest

from backend.notifications.watchdog import (
    BUILTIN_RULES,
    _rule_batch_stalled,
    _rule_gpu_idle_during_training,
    _rule_oom_kill_suspected,
    _rule_val_loss_diverging,
)


# ---------------------------------------------------------------------------
# Helpers — plain namespace stubs (no SQLAlchemy)
# ---------------------------------------------------------------------------


def _now_iso(offset_s: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_s)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _batch(status: str = "running", host: str = "gpu-1") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id="batch-test", status=status, host=host, owner_id=1
    )


def _job(job_id: str = "j1", status: str = "running") -> types.SimpleNamespace:
    return types.SimpleNamespace(id=job_id, batch_id="batch-test", status=status)


def _epoch_event(job_id: str, val_loss: float, offset_s: int = 0) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        batch_id="batch-test",
        job_id=job_id,
        event_type="job_epoch",
        timestamp=_now_iso(offset_s),
        schema_version="1.1",
        data=json.dumps({"val_loss": val_loss, "epoch": 1}),
    )


def _gpu_util_event(util: float) -> types.SimpleNamespace:
    """Synthetic gpu_util pseudo-event injected by the scan loop."""
    return types.SimpleNamespace(
        batch_id="batch-test",
        job_id=None,
        event_type="__gpu_util__",
        timestamp=_now_iso(),
        schema_version="internal",
        data=str(util),
    )


def _job_done_event(offset_s: int = 0) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        batch_id="batch-test",
        job_id="j1",
        event_type="job_done",
        timestamp=_now_iso(offset_s),
        schema_version="1.1",
        data=json.dumps({"status": "DONE"}),
    )


def _log_line_event(job_id: str, line: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        batch_id="batch-test",
        job_id=job_id,
        event_type="log_line",
        timestamp=_now_iso(),
        schema_version="1.1",
        data=json.dumps({"line": line}),
    )


# ---------------------------------------------------------------------------
# Rule: val_loss_diverging
# ---------------------------------------------------------------------------


class TestValLossDiverging:
    """The rule fires when 3 consecutive job_epoch events per job show
    strictly increasing val_loss AND newest/oldest ratio > 1.3.
    """

    def test_fires_on_strictly_increasing_above_threshold(self) -> None:
        batch = _batch()
        jobs = [_job()]
        # oldest → newest when reversed (events passed newest-first)
        events = [
            _epoch_event("j1", val_loss=0.80, offset_s=-20),  # newest
            _epoch_event("j1", val_loss=0.60, offset_s=-40),
            _epoch_event("j1", val_loss=0.45, offset_s=-60),  # oldest
        ]
        # 0.80 / 0.45 ≈ 1.78 > 1.3 and strictly increasing
        assert _rule_val_loss_diverging(batch, jobs, events) is True

    def test_does_not_fire_below_ratio(self) -> None:
        """Ratio 0.52/0.50 = 1.04 < 1.3 → no fire."""
        batch = _batch()
        events = [
            _epoch_event("j1", 0.52, -10),
            _epoch_event("j1", 0.51, -20),
            _epoch_event("j1", 0.50, -30),
        ]
        assert _rule_val_loss_diverging(batch, [], events) is False

    def test_does_not_fire_when_loss_decreasing(self) -> None:
        batch = _batch()
        events = [
            _epoch_event("j1", 0.30, -10),
            _epoch_event("j1", 0.35, -20),
            _epoch_event("j1", 0.40, -30),
        ]
        assert _rule_val_loss_diverging(batch, [], events) is False

    def test_does_not_fire_with_fewer_than_3_epochs(self) -> None:
        batch = _batch()
        events = [
            _epoch_event("j1", 0.80, -10),
            _epoch_event("j1", 0.60, -20),
        ]
        assert _rule_val_loss_diverging(batch, [], events) is False

    def test_does_not_fire_with_no_events(self) -> None:
        assert _rule_val_loss_diverging(_batch(), [], []) is False


# ---------------------------------------------------------------------------
# Rule: gpu_idle_during_training
# ---------------------------------------------------------------------------


class TestGpuIdleDuringTraining:
    """Fires when batch=running, last 3 gpu_util < 5, no recent job_done."""

    def test_fires_on_three_idle_snapshots(self) -> None:
        batch = _batch(status="running", host="gpu-1")
        events = [
            _gpu_util_event(2.0),
            _gpu_util_event(1.5),
            _gpu_util_event(0.0),
        ]
        assert _rule_gpu_idle_during_training(batch, [], events) is True

    def test_does_not_fire_when_batch_not_running(self) -> None:
        batch = _batch(status="done")
        events = [_gpu_util_event(0.0)] * 3
        assert _rule_gpu_idle_during_training(batch, [], events) is False

    def test_does_not_fire_when_one_snapshot_high(self) -> None:
        batch = _batch(status="running")
        events = [
            _gpu_util_event(90.0),  # active
            _gpu_util_event(1.0),
            _gpu_util_event(0.0),
        ]
        assert _rule_gpu_idle_during_training(batch, [], events) is False

    def test_does_not_fire_when_recent_job_done(self) -> None:
        """If a job finished in the last 10 minutes, don't fire."""
        batch = _batch(status="running")
        events = [
            _gpu_util_event(0.0),
            _gpu_util_event(0.0),
            _gpu_util_event(0.0),
            _job_done_event(offset_s=-60),  # 1 minute ago — within 10 min
        ]
        assert _rule_gpu_idle_during_training(batch, [], events) is False

    def test_does_not_fire_with_no_host(self) -> None:
        batch = _batch(status="running", host="")
        events = [_gpu_util_event(0.0)] * 3
        assert _rule_gpu_idle_during_training(batch, [], events) is False

    def test_does_not_fire_with_fewer_than_3_snapshots(self) -> None:
        batch = _batch(status="running")
        events = [
            _gpu_util_event(0.0),
            _gpu_util_event(0.0),
        ]
        assert _rule_gpu_idle_during_training(batch, [], events) is False


# ---------------------------------------------------------------------------
# Rule: batch_stalled
# ---------------------------------------------------------------------------


class TestBatchStalled:
    """Fires when latest event is older than max(5 min, 2× median epoch time)."""

    def test_fires_when_last_event_is_old(self) -> None:
        batch = _batch(status="running")
        # Single old event, no epoch events → default threshold 300 s.
        ev = _job_done_event(offset_s=-400)  # 400 s ago > 300 s threshold
        ev.event_type = "batch_start"
        assert _rule_batch_stalled(batch, [], [ev]) is True

    def test_does_not_fire_when_recent_event(self) -> None:
        batch = _batch(status="running")
        ev = _job_done_event(offset_s=-30)  # 30 s ago
        ev.event_type = "job_epoch"
        assert _rule_batch_stalled(batch, [], [ev]) is False

    def test_does_not_fire_when_batch_not_running(self) -> None:
        batch = _batch(status="done")
        ev = _job_done_event(offset_s=-9999)
        assert _rule_batch_stalled(batch, [], [ev]) is False

    def test_does_not_fire_with_no_events(self) -> None:
        assert _rule_batch_stalled(_batch(), [], []) is False

    def test_threshold_scales_with_epoch_time(self) -> None:
        """With 200 s median epoch time, threshold becomes 400 s.

        events[0] at -650 s, events[1] at -450 s, events[2] at -250 s.
        Latest is 250 s ago; threshold = max(300, 2*200) = 400 s.
        250 < 400 → should NOT fire.
        """
        batch = _batch(status="running")
        base = datetime.now(timezone.utc)
        events = []
        for i in range(3):
            ts = base - timedelta(seconds=650 - i * 200)
            ev = types.SimpleNamespace(
                batch_id="batch-test",
                job_id="j1",
                event_type="job_epoch",
                timestamp=ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                schema_version="1.1",
                data=json.dumps({"val_loss": 0.3}),
            )
            events.append(ev)
        # Pass newest-first (reverse the list we built oldest-first)
        assert _rule_batch_stalled(batch, [], list(reversed(events))) is False


# ---------------------------------------------------------------------------
# Rule: oom_kill_suspected
# ---------------------------------------------------------------------------


class TestOomKillSuspected:
    """Fires when a failed job has a log_line matching OOM patterns."""

    def test_fires_on_out_of_memory_log(self) -> None:
        batch = _batch()
        jobs = [_job("j1", status="failed")]
        events = [
            _log_line_event("j1", "RuntimeError: CUDA out of memory. Tried to allocate 2 GB"),
        ]
        assert _rule_oom_kill_suspected(batch, jobs, events) is True

    def test_fires_on_cuda_allocate_log(self) -> None:
        batch = _batch()
        jobs = [_job("j1", status="failed")]
        events = [
            _log_line_event("j1", "CUDA error: unable to allocate 512 MiB"),
        ]
        assert _rule_oom_kill_suspected(batch, jobs, events) is True

    def test_does_not_fire_without_failed_jobs(self) -> None:
        batch = _batch()
        jobs = [_job("j1", status="running")]
        events = [
            _log_line_event("j1", "CUDA out of memory"),
        ]
        assert _rule_oom_kill_suspected(batch, jobs, events) is False

    def test_does_not_fire_on_unrelated_log(self) -> None:
        batch = _batch()
        jobs = [_job("j1", status="failed")]
        events = [
            _log_line_event("j1", "Training step 42 completed"),
        ]
        assert _rule_oom_kill_suspected(batch, jobs, events) is False

    def test_does_not_fire_with_no_jobs(self) -> None:
        assert _rule_oom_kill_suspected(_batch(), [], []) is False

    def test_does_not_fire_when_oom_from_non_failed_job(self) -> None:
        batch = _batch()
        jobs = [_job("j1", status="running"), _job("j2", status="failed")]
        # j1 has the OOM line but is running; j2 (failed) has no log.
        events = [
            _log_line_event("j1", "CUDA out of memory"),
        ]
        assert _rule_oom_kill_suspected(batch, jobs, events) is False


# ---------------------------------------------------------------------------
# Registry sanity check
# ---------------------------------------------------------------------------


def test_builtin_rules_count() -> None:
    """Exactly 4 built-in rules must be registered."""
    assert len(BUILTIN_RULES) == 4


def test_builtin_rule_ids_unique() -> None:
    ids = [r.id for r in BUILTIN_RULES]
    assert len(ids) == len(set(ids))
