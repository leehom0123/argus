"""``job_artifact`` multipart upload — happy path + defensive failure modes."""
from __future__ import annotations

import json

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from argus import ExperimentReporter


@pytest.fixture
def artifact_server(httpserver: HTTPServer, tmp_path):
    received: list[dict] = []

    def handler(request):
        # Multipart request — introspect headers + form + files.
        received.append(
            {
                "path": request.path,
                "content_type": request.headers.get("Content-Type", ""),
                "form": dict(request.form.items()),
                "filenames": [f.filename for f in request.files.values()],
                "file_bytes": {
                    name: f.read() for name, f in request.files.items()
                },
            }
        )
        return Response(
            json.dumps({"id": 1, "size_bytes": 1}),
            status=200,
            content_type="application/json",
        )

    httpserver.expect_request(
        "/api/jobs/job-42/artifacts", method="POST"
    ).respond_with_handler(handler)
    return httpserver, received


def _mk_reporter(url: str):
    return ExperimentReporter(
        url=url,
        project="p",
        auth_token="em_live_tok",
        host="h",
        user="u",
        commit="abc",
        timeout=2.0,
        queue_size=10,
    )


def test_job_artifact_uploads_multipart(artifact_server, tmp_path):
    server, received = artifact_server
    png = tmp_path / "prediction_comparison.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n\x00BODY")

    rep = _mk_reporter(server.url_for("").rstrip("/"))
    try:
        rep.job_artifact(
            "job-42", png, label="visualizations", meta={"step": 1}
        )
    finally:
        rep.close(timeout=2.0)

    assert len(received) == 1
    req = received[0]
    assert req["path"] == "/api/jobs/job-42/artifacts"
    assert req["content_type"].startswith("multipart/form-data")
    assert req["form"]["label"] == "visualizations"
    assert json.loads(req["form"]["meta"]) == {"step": 1}
    assert req["filenames"] == ["prediction_comparison.png"]
    assert req["file_bytes"]["file"] == b"\x89PNG\r\n\x1a\n\x00BODY"


def test_job_artifact_missing_file_swallowed(artifact_server):
    server, received = artifact_server
    rep = _mk_reporter(server.url_for("").rstrip("/"))
    try:
        # Must not raise even though path does not exist.
        rep.job_artifact("job-42", "/tmp/does-not-exist-xyz.bin", label="v")
    finally:
        rep.close(timeout=2.0)
    assert received == []


def test_job_artifact_server_500_swallowed(httpserver: HTTPServer, tmp_path):
    httpserver.expect_request(
        "/api/jobs/job-42/artifacts", method="POST"
    ).respond_with_data("boom", status=500)
    blob = tmp_path / "pred.csv"
    blob.write_text("a,b\n1,2\n")
    rep = _mk_reporter(httpserver.url_for("").rstrip("/"))
    try:
        # 5xx must not raise — training is sacred.
        rep.job_artifact("job-42", blob)
    finally:
        rep.close(timeout=2.0)
