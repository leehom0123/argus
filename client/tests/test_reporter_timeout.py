"""Slow server + aggressive client timeout: events still spill, no exception."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from argus import ExperimentReporter


@pytest.fixture
def slow_server(httpserver: HTTPServer):
    def slow_handler(request):
        time.sleep(3.0)  # much longer than client timeout
        return Response(json.dumps({"accepted": True}), status=200)

    httpserver.expect_request("/api/events", method="POST").respond_with_handler(slow_handler)
    return httpserver


@pytest.mark.timeout(30)
def test_spill_on_timeout(slow_server, tmp_path):
    spill = tmp_path / "spill.jsonl"
    rep = ExperimentReporter(
        url=slow_server.url_for("").rstrip("/"),
        project="test-project",
        timeout=0.2,
        spill_path=str(spill),
    )
    rep.batch_start(experiment_type="forecast", n_total=1)
    rep.job_start(job_id="j1", model="m", dataset="d")
    # Close gives worker time for retries; retry budget ≈ 0.1+0.3+1.0 = 1.4s per event.
    rep.close(timeout=20.0)

    assert spill.exists(), "spill must exist after timeouts"
    with open(spill, "r", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]
    assert len(lines) >= 2
