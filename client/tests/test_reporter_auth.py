"""Auth behaviour: token in header; missing token is flagged but non-fatal."""
from __future__ import annotations

import logging
import time

from argus import ExperimentReporter


def test_bearer_header_present(mock_server, received_requests):
    rep = ExperimentReporter(
        url=mock_server.url_for("").rstrip("/"),
        project="test-project",
        auth_token="em_live_abc123",
        timeout=1.0,
    )
    rep.batch_start(experiment_type="forecast", n_total=1)
    rep.batch_done(n_done=1, n_failed=0)
    rep.close(timeout=3.0)

    # at least one POST must have Authorization: Bearer em_live_abc123
    seen = [r["headers"].get("Authorization") for r in received_requests]
    assert any(v == "Bearer em_live_abc123" for v in seen), seen


def test_missing_token_logs_error_but_does_not_raise(caplog, mock_server):
    """At init with auth_token=None we log a structured error, but training is sacred."""
    with caplog.at_level(logging.ERROR, logger="argus"):
        rep = ExperimentReporter(
            url=mock_server.url_for("").rstrip("/"),
            project="test-project",
            timeout=1.0,
        )
    try:
        rep.batch_start(experiment_type="forecast", n_total=1)
        rep.batch_done(n_done=1, n_failed=0)
    finally:
        rep.close(timeout=3.0)

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("auth_token not provided" in m for m in msgs), msgs


def test_401_drops_without_retry(httpserver, tmp_path):
    """401 Unauthorized should log error and drop — no retry, no spill."""
    from werkzeug.wrappers import Response
    hits = {"n": 0}

    def handler(request):
        hits["n"] += 1
        return Response('{"detail":"bad token"}', status=401,
                        content_type="application/json")

    httpserver.expect_request("/api/events", method="POST").respond_with_handler(handler)

    spill = tmp_path / "spill.jsonl"
    rep = ExperimentReporter(
        url=httpserver.url_for("").rstrip("/"),
        project="test-project",
        auth_token="em_live_wrong",
        timeout=1.0,
        spill_path=str(spill),
    )
    rep.batch_start(experiment_type="forecast", n_total=1)
    rep.close(timeout=3.0)

    # Single POST, no backoff retries, no spill
    assert hits["n"] == 1
    assert not spill.exists(), "401 should drop, not spill"
