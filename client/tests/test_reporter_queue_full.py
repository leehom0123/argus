"""Queue saturation: drops get logged, process does not crash."""
from __future__ import annotations

import logging
import time

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from argus import ExperimentReporter


@pytest.fixture
def paused_server(httpserver: HTTPServer):
    def slow(request):
        time.sleep(0.5)
        return Response('{"accepted": true}', status=200)

    httpserver.expect_request("/api/events", method="POST").respond_with_handler(slow)
    return httpserver


def test_drop_oldest_logs_warning(paused_server, tmp_path, caplog):
    spill = tmp_path / "spill.jsonl"
    rep = ExperimentReporter(
        url=paused_server.url_for("").rstrip("/"),
        project="test-project",
        timeout=0.3,
        queue_size=5,
        spill_path=str(spill),
    )
    with caplog.at_level(logging.WARNING, logger="argus"):
        rep.batch_start(experiment_type="forecast", n_total=100)
        rep.job_start(job_id="j1", model="m", dataset="d")
        for i in range(100):
            rep.job_epoch(job_id="j1", epoch=i, train_loss=0.1)
        # we don't strictly need to close; just let the queue overflow
        # happen during the rapid push above
        rep.close(timeout=10.0)

    # Some drops must have happened (queue only held 5 slots while worker was slow)
    assert rep._drops > 0, "queue should have overflowed at least once"
    # Warnings logged at least once
    queue_warnings = [
        r for r in caplog.records if "queue full" in r.getMessage()
    ]
    assert queue_warnings, "expected queue-full warnings to be logged"
