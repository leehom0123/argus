"""Each emitted event carries a unique client-generated UUID event_id."""
from __future__ import annotations

import time
import uuid


def _wait_for(received, n, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(received) >= n:
            return True
        time.sleep(0.05)
    return False


def test_every_event_has_unique_uuid(reporter, received_events):
    reporter.batch_start(experiment_type="forecast", n_total=1)
    reporter.job_start(job_id="j1", model="m", dataset="d")
    for i in range(10):
        reporter.job_epoch(job_id="j1", epoch=i, train_loss=0.1)
    reporter.close(timeout=3.0)
    assert _wait_for(received_events, 12, timeout=3.0), (
        f"only got {len(received_events)} events"
    )

    ids = [e["event_id"] for e in received_events]
    # every id is a valid UUID
    for eid in ids:
        uuid.UUID(eid)
    # all distinct
    assert len(set(ids)) == len(ids), f"duplicate event_ids: {ids}"


def test_repeated_method_calls_yield_different_ids(reporter, received_events):
    """Confirms the spec requirement: job_epoch(...) back to back -> distinct UUIDs."""
    reporter.batch_start(experiment_type="forecast", n_total=1)
    reporter.job_start(job_id="j1", model="m", dataset="d")
    # Intentionally identical args
    for _ in range(5):
        reporter.job_epoch(job_id="j1", epoch=0, train_loss=0.5, val_loss=0.5, lr=1e-4)
    reporter.close(timeout=3.0)

    epochs = [e for e in received_events if e["event_type"] == "job_epoch"]
    assert len(epochs) == 5
    eids = [e["event_id"] for e in epochs]
    assert len(set(eids)) == 5, f"expected 5 distinct ids, got {eids}"
