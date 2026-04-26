"""Pre-populate a spill file, then start a Reporter; it should replay them."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from argus import ExperimentReporter
from argus.schema import SCHEMA_VERSION


def _make_event(event_type: str, batch_id: str, job_id=None) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "timestamp": "2026-04-23T10:00:00Z",
        "batch_id": batch_id,
        "job_id": job_id,
        "source": {"project": "test-project"},
        "data": {"replayed": True},
    }


def test_replay_spilled_events(mock_server, received_events, tmp_path):
    spill = tmp_path / "spill.jsonl"
    with open(spill, "w", encoding="utf-8") as f:
        f.write(json.dumps(_make_event("batch_start", "old-batch-1")) + "\n")
        f.write(json.dumps(_make_event("job_start", "old-batch-1", "j1")) + "\n")
        f.write(json.dumps(_make_event("job_done", "old-batch-1", "j1")) + "\n")

    rep = ExperimentReporter(
        url=mock_server.url_for("").rstrip("/"),
        project="test-project",
        timeout=3.0,
        spill_path=str(spill),
    )
    # give the worker a tick to replay
    deadline = time.time() + 10.0
    while time.time() < deadline and len(received_events) < 3:
        time.sleep(0.1)
    rep.close(timeout=5.0)

    assert len(received_events) >= 3
    replayed = [e for e in received_events if e.get("data", {}).get("replayed") is True]
    # At-least-once delivery semantics: a flaky first POST may be retried,
    # so we assert >= 3 rather than == 3. Backend is expected to dedupe.
    assert len(replayed) >= 3
    event_types = {e["event_type"] for e in replayed}
    assert event_types >= {"batch_start", "job_start", "job_done"}
    # spill file should be consumed
    assert not spill.exists(), "spill file should be removed after successful drain"


def test_malformed_line_skipped(mock_server, received_events, tmp_path):
    spill = tmp_path / "spill.jsonl"
    with open(spill, "w", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps(_make_event("batch_start", "b1")) + "\n")
        f.write("{broken\n")

    rep = ExperimentReporter(
        url=mock_server.url_for("").rstrip("/"),
        project="test-project",
        timeout=1.0,
        spill_path=str(spill),
    )
    deadline = time.time() + 3.0
    while time.time() < deadline and len(received_events) < 1:
        time.sleep(0.1)
    rep.close(timeout=2.0)

    assert len(received_events) == 1
    assert received_events[0]["event_type"] == "batch_start"
