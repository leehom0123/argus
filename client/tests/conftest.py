"""Shared fixtures: mock HTTP server, schema loader, reporter factory.

Mock server understands both:
  POST /api/events        -> single event in body
  POST /api/events/batch  -> {"events": [...]}
In both cases every event is appended to `received_events` so tests can
assert on the full emitted sequence regardless of which endpoint was used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest
from pytest_httpserver import HTTPServer

from argus import ExperimentReporter

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "event_v1.json"


@pytest.fixture
def event_schema() -> Dict[str, Any]:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def received_events() -> List[Dict[str, Any]]:
    """Collects events the mock server receives (unwraps batch payloads)."""
    return []


@pytest.fixture
def received_requests() -> List[Dict[str, Any]]:
    """Collects {'path': ..., 'headers': ..., 'body': ...} for each POST.
    Useful for asserting which endpoint and headers were used.
    """
    return []


@pytest.fixture
def mock_server(httpserver: HTTPServer, received_events, received_requests):
    """HTTP server that handles both single and batch event endpoints."""
    from werkzeug.wrappers import Response

    def single_handler(request):
        headers = dict(request.headers.items())
        try:
            body = json.loads(request.get_data(as_text=True))
        except Exception:
            body = {"_parse_error": True}
        received_requests.append({
            "path": request.path, "headers": headers, "body": body,
        })
        received_events.append(body)
        eid = body.get("event_id") if isinstance(body, dict) else None
        return Response(
            json.dumps({"accepted": True, "event_id": eid}),
            status=200, content_type="application/json",
        )

    def batch_handler(request):
        headers = dict(request.headers.items())
        try:
            body = json.loads(request.get_data(as_text=True))
        except Exception:
            body = {"_parse_error": True}
        received_requests.append({
            "path": request.path, "headers": headers, "body": body,
        })
        events = (body or {}).get("events", []) if isinstance(body, dict) else []
        results = []
        for ev in events:
            received_events.append(ev)
            results.append({
                "event_id": ev.get("event_id") if isinstance(ev, dict) else None,
                "status": "accepted", "db_id": len(received_events),
            })
        return Response(
            json.dumps({"accepted": len(results), "rejected": 0, "results": results}),
            status=200, content_type="application/json",
        )

    httpserver.expect_request("/api/events", method="POST").respond_with_handler(single_handler)
    httpserver.expect_request("/api/events/batch", method="POST").respond_with_handler(batch_handler)
    return httpserver


@pytest.fixture
def reporter_url(mock_server: HTTPServer) -> str:
    return mock_server.url_for("").rstrip("/")


@pytest.fixture
def tmp_spill(tmp_path: Path) -> Path:
    return tmp_path / "spill.jsonl"


@pytest.fixture
def reporter(reporter_url: str, tmp_spill: Path):
    # Ensure the disable env is NOT set during tests.
    os.environ.pop("ARGUS_DISABLE", None)
    rep = ExperimentReporter(
        url=reporter_url,
        project="test-project",
        auth_token="em_live_test_token",
        host="test-host",
        user="tester",
        commit="deadbee",
        timeout=1.0,
        queue_size=100,
        spill_path=str(tmp_spill),
    )
    yield rep
    rep.close(timeout=2.0)
