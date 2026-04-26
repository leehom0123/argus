"""Dedicated test for ARGUS_DISABLE=1 as an explicit spec item."""
from __future__ import annotations

import time

from argus import ExperimentReporter


def test_disable_env_makes_everything_noop(monkeypatch, mock_server, received_events, tmp_path):
    monkeypatch.setenv("ARGUS_DISABLE", "1")
    spill = tmp_path / "spill.jsonl"
    rep = ExperimentReporter(
        url=mock_server.url_for("").rstrip("/"),
        project="test-project",
        auth_token="em_live_xxx",
        spill_path=str(spill),
    )
    # exercise every public method
    bid = rep.batch_start(experiment_type="forecast", n_total=3)
    assert isinstance(bid, str)
    rep.job_start(job_id="j1", model="m", dataset="d")
    rep.job_epoch(job_id="j1", epoch=0, train_loss=0.1)
    rep.resource_snapshot(gpu_util_pct=10)
    rep.log_line(job_id="j1", line="hello")
    rep.job_done(job_id="j1", metrics={"MSE": 0.1}, elapsed_s=1.0)
    rep.job_failed(job_id="j2", reason="nope")
    rep.batch_failed(reason="cancelled")
    rep.batch_done(n_done=1, n_failed=0)
    rep.close(timeout=1.0)

    time.sleep(0.3)
    assert received_events == [], "disabled reporter must not hit the network"
    # no spill either
    assert not spill.exists(), "disabled reporter must not spill"


def test_disable_env_accepts_various_truthy_values(monkeypatch):
    from argus.reporter import _is_disabled
    for val in ["1", "true", "TRUE", "yes", "on"]:
        monkeypatch.setenv("ARGUS_DISABLE", val)
        assert _is_disabled(), val
    for val in ["0", "false", "", "no", "off"]:
        monkeypatch.setenv("ARGUS_DISABLE", val)
        assert not _is_disabled(), val
