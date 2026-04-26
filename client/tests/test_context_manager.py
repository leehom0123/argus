"""`with ExperimentReporter(...) as rep:` auto-closes on exit."""
from __future__ import annotations

import time

from argus import ExperimentReporter


def test_context_manager_drains(mock_server, received_events, tmp_path):
    spill = tmp_path / "spill.jsonl"
    with ExperimentReporter(
        url=mock_server.url_for("").rstrip("/"),
        project="test-project",
        timeout=1.0,
        spill_path=str(spill),
    ) as rep:
        bid = rep.batch_start(experiment_type="forecast", n_total=1)
        rep.job_start(job_id="j1", model="m", dataset="d")
        rep.job_done(job_id="j1", metrics={"MSE": 0.1}, elapsed_s=0.1)
        rep.batch_done(n_done=1, n_failed=0)
    # after __exit__ the worker should have drained
    deadline = time.time() + 3.0
    while time.time() < deadline and len(received_events) < 4:
        time.sleep(0.1)
    assert len(received_events) == 4
    assert all(e["batch_id"] == bid for e in received_events)


def test_context_manager_even_on_exception(mock_server, received_events, tmp_path):
    spill = tmp_path / "spill.jsonl"
    try:
        with ExperimentReporter(
            url=mock_server.url_for("").rstrip("/"),
            project="test-project",
            timeout=1.0,
            spill_path=str(spill),
        ) as rep:
            rep.batch_start(experiment_type="forecast", n_total=1)
            rep.job_start(job_id="j1", model="m", dataset="d")
            raise RuntimeError("simulated user error")
    except RuntimeError:
        pass

    # the two events pushed before the raise should still be delivered
    deadline = time.time() + 3.0
    while time.time() < deadline and len(received_events) < 2:
        time.sleep(0.1)
    assert len(received_events) >= 2
