"""Backend returns 500 for every request. Events should end up spilled, never raised."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from argus import ExperimentReporter


@pytest.fixture
def failing_server(httpserver: HTTPServer):
    httpserver.expect_request("/api/events", method="POST").respond_with_data(
        "server error", status=500
    )
    return httpserver


def test_spill_on_500(failing_server, tmp_path):
    spill = tmp_path / "spill.jsonl"
    rep = ExperimentReporter(
        url=failing_server.url_for("").rstrip("/"),
        project="test-project",
        timeout=0.5,
        spill_path=str(spill),
    )
    # Caller-side methods must not raise.
    bid = rep.batch_start(experiment_type="forecast", n_total=1)
    rep.job_start(job_id="j1", model="m", dataset="d")
    rep.job_done(job_id="j1", metrics={"MSE": 0.5}, elapsed_s=1.0)
    rep.batch_done(n_done=1, n_failed=0)

    # close() must block until either drained or timeout; here retries take time so
    # give it enough slack.
    rep.close(timeout=15.0)

    assert spill.exists(), "spill file must exist after all retries fail"
    with open(spill, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) >= 4, f"expected ≥4 spilled events, got {len(lines)}"
    types = [e["event_type"] for e in lines]
    assert "batch_start" in types
    assert "job_start" in types
    assert "job_done" in types
    assert "batch_done" in types
    for e in lines:
        assert e["schema_version"] == "1.1"
        assert e["batch_id"] == bid
        assert "event_id" in e


def test_caller_never_raises_even_on_invalid_url(tmp_path):
    """Unreachable host should not crash the caller; events go to spill."""
    spill = tmp_path / "spill.jsonl"
    rep = ExperimentReporter(
        url="http://127.0.0.1:1",  # nothing listens here
        project="test-project",
        timeout=0.2,
        spill_path=str(spill),
    )
    rep.batch_start(experiment_type="forecast", n_total=1)
    rep.job_start(job_id="j1", model="m", dataset="d")
    rep.job_done(job_id="j1", metrics={}, elapsed_s=0.1)
    rep.batch_done(n_done=1, n_failed=0)
    rep.close(timeout=10.0)
    assert spill.exists()
