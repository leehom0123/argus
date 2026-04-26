"""When many events queue up at once, the worker ships them via the
batch endpoint (/api/events/batch) instead of one POST per event.
"""
from __future__ import annotations

import time

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from argus import ExperimentReporter


@pytest.fixture
def dual_endpoint_server(httpserver: HTTPServer):
    """Slow handler so the queue actually accumulates, logging which path was hit."""
    import json as _json
    calls = {"single": 0, "batch": 0, "single_bodies": [], "batch_bodies": []}

    def single(request):
        time.sleep(0.25)  # give queue time to accumulate
        calls["single"] += 1
        try:
            calls["single_bodies"].append(_json.loads(request.get_data(as_text=True)))
        except Exception:
            pass
        return Response('{"accepted": true}', status=200,
                        content_type="application/json")

    def batch(request):
        calls["batch"] += 1
        try:
            calls["batch_bodies"].append(_json.loads(request.get_data(as_text=True)))
        except Exception:
            pass
        return Response('{"accepted": 100, "rejected": 0, "results": []}',
                        status=200, content_type="application/json")

    httpserver.expect_request("/api/events", method="POST").respond_with_handler(single)
    httpserver.expect_request("/api/events/batch", method="POST").respond_with_handler(batch)
    httpserver._calls = calls  # stash
    return httpserver


def test_batch_endpoint_used_on_burst(dual_endpoint_server, tmp_path):
    spill = tmp_path / "spill.jsonl"
    rep = ExperimentReporter(
        url=dual_endpoint_server.url_for("").rstrip("/"),
        project="test-project",
        auth_token="em_live_xxx",
        timeout=2.0,
        queue_size=200,
        spill_path=str(spill),
    )
    rep.batch_start(experiment_type="forecast", n_total=1)
    rep.job_start(job_id="j1", model="m", dataset="d")
    # Burst 50 epochs — more than the flush threshold.
    for i in range(50):
        rep.job_epoch(job_id="j1", epoch=i, train_loss=0.1)
    rep.job_done(job_id="j1", metrics={"MSE": 0.1}, elapsed_s=1.0)
    rep.batch_done(n_done=1, n_failed=0)
    rep.close(timeout=10.0)

    calls = dual_endpoint_server._calls
    assert calls["batch"] >= 1, (
        f"expected at least one batch POST; got single={calls['single']}, batch={calls['batch']}"
    )
    # The batch body must have the expected envelope.
    for body in calls["batch_bodies"]:
        assert "events" in body and isinstance(body["events"], list)
        for ev in body["events"]:
            assert ev["schema_version"] == "1.1"
            assert "event_id" in ev
