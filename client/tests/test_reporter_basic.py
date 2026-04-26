"""End-to-end happy path: all 6 events arrive, schema-valid, batch_id threaded."""
from __future__ import annotations

import time

import jsonschema
import pytest


def _wait_for(received, n, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(received) >= n:
            return True
        time.sleep(0.05)
    return False


def test_full_flow(reporter, received_events, event_schema):
    batch_id = reporter.batch_start(
        experiment_type="forecast",
        n_total=1,
        command="python main.py experiment=dam_forecast",
    )
    assert batch_id.startswith("batch-")

    reporter.job_start(job_id="etth1_transformer", model="transformer", dataset="etth1")
    for epoch in range(3):
        reporter.job_epoch(
            job_id="etth1_transformer",
            epoch=epoch,
            train_loss=0.5 - 0.1 * epoch,
            val_loss=0.6 - 0.1 * epoch,
            lr=1e-4,
        )
    reporter.job_done(
        job_id="etth1_transformer",
        metrics={"MSE": 0.44, "MAE": 0.42},
        elapsed_s=20.5,
        train_epochs=3,
    )
    reporter.batch_done(n_done=1, n_failed=0, total_elapsed_s=20.5)
    reporter.close(timeout=3.0)

    assert _wait_for(received_events, 6, timeout=3.0), (
        f"only got {len(received_events)} events"
    )
    types = [e["event_type"] for e in received_events]
    assert types == [
        "batch_start",
        "job_start",
        "job_epoch",
        "job_epoch",
        "job_epoch",
        "job_done",
    ] or types[:6] == [
        "batch_start",
        "job_start",
        "job_epoch",
        "job_epoch",
        "job_epoch",
        "job_done",
    ]
    # batch_done may be 7th; confirm at least one batch_done present too
    assert any(e["event_type"] == "batch_done" for e in received_events)

    for ev in received_events:
        assert ev["schema_version"] == "1.1"
        assert ev["batch_id"] == batch_id
        assert ev["source"]["project"] == "test-project"
        # every event carries a UUID event_id
        assert "event_id" in ev
        import uuid as _uuid
        _uuid.UUID(ev["event_id"])
        jsonschema.validate(ev, event_schema)


def test_auto_batch_id_uuid(reporter, received_events):
    bid = reporter.batch_start(experiment_type="forecast", n_total=2)
    assert isinstance(bid, str) and len(bid) > 5
    reporter.batch_done(n_done=2, n_failed=0)
    reporter.close(timeout=3.0)
    assert _wait_for(received_events, 2)


def test_explicit_batch_id_threaded(reporter, received_events, event_schema):
    reporter.batch_start(
        experiment_type="forecast",
        n_total=1,
        batch_id="my-custom-batch-001",
    )
    reporter.job_start(job_id="j1", model="m", dataset="d")
    reporter.job_failed(job_id="j1", reason="oom", elapsed_s=1.0)
    reporter.close(timeout=3.0)
    _wait_for(received_events, 3)
    for ev in received_events:
        assert ev["batch_id"] == "my-custom-batch-001"
        jsonschema.validate(ev, event_schema)


def test_resource_snapshot_and_log_line(reporter, received_events, event_schema):
    reporter.batch_start(experiment_type="forecast", n_total=1)
    reporter.resource_snapshot(gpu_util_pct=95, gpu_mem_mb=20000, ram_mb=40000)
    reporter.job_start(job_id="j1", model="m", dataset="d")
    reporter.log_line(job_id="j1", line="Epoch 1 starting", level="info")
    reporter.close(timeout=3.0)
    _wait_for(received_events, 4)
    types = {e["event_type"] for e in received_events}
    assert "resource_snapshot" in types
    assert "log_line" in types
    for ev in received_events:
        jsonschema.validate(ev, event_schema)


def test_event_before_batch_start_is_dropped(reporter_url, tmp_spill, received_events):
    """job_* calls before batch_start() must be silently dropped, not crash."""
    from argus import ExperimentReporter
    rep = ExperimentReporter(
        url=reporter_url,
        project="test-project",
        timeout=1.0,
        spill_path=str(tmp_spill),
    )
    try:
        # No batch_start yet
        rep.job_start(job_id="j1", model="m", dataset="d")
        rep.job_epoch(job_id="j1", epoch=0, train_loss=0.1)
    finally:
        rep.close(timeout=2.0)
    assert len(received_events) == 0


def test_disabled_via_env(reporter_url, tmp_spill, received_events, monkeypatch):
    monkeypatch.setenv("ARGUS_DISABLE", "1")
    from argus import ExperimentReporter
    rep = ExperimentReporter(
        url=reporter_url,
        project="test-project",
        timeout=1.0,
        spill_path=str(tmp_spill),
    )
    bid = rep.batch_start(experiment_type="forecast", n_total=1)
    assert isinstance(bid, str)
    rep.job_start(job_id="j1", model="m", dataset="d")
    rep.job_done(job_id="j1", metrics={"MSE": 0.4}, elapsed_s=1.0)
    rep.batch_done(n_done=1, n_failed=0)
    rep.close(timeout=1.0)
    # Nothing should be sent to the mock server
    time.sleep(0.3)
    assert received_events == []
