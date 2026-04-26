"""Example: wrap a run_benchmark-style driver.

Pattern: the orchestrator opens one Reporter for the whole sweep,
calls batch_start once, then delegates each training subprocess to
emit job_* events itself — or, if that's invasive, parses their
stdout/finish status and emits job_* centrally.

This file shows the "central emitter" variant (no changes required
inside the training script).
"""
from __future__ import annotations

import subprocess
import time
from typing import List

from argus import ExperimentReporter


def run_one_job(job_id: str, cmd: List[str], rep: ExperimentReporter) -> bool:
    rep.job_start(job_id=job_id, model=cmd[0], dataset="unknown")
    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        elapsed = time.time() - t0
        if result.returncode == 0:
            # Parse metrics from stdout / a results file here in real usage.
            rep.job_done(job_id=job_id, metrics={}, elapsed_s=elapsed)
            return True
        rep.job_failed(
            job_id=job_id,
            reason=f"exit code {result.returncode}: {result.stderr[-300:]}",
            elapsed_s=elapsed,
        )
        return False
    except subprocess.TimeoutExpired:
        rep.job_failed(job_id=job_id, reason="timeout", elapsed_s=time.time() - t0)
        return False


def main():
    jobs = [
        ("etth1_transformer", ["echo", "job1"]),
        ("etth2_patchtst",    ["echo", "job2"]),
    ]
    t0 = time.time()
    n_done = n_failed = 0
    with ExperimentReporter(
        url="http://localhost:9999",
        project="DeepTS-Flow-Wheat",
        timeout=1.0,
    ) as rep:
        rep.batch_start(
            experiment_type="forecast",
            n_total=len(jobs),
            command="python examples/benchmark_wrapper.py",
        )
        for jid, cmd in jobs:
            if run_one_job(jid, cmd, rep):
                n_done += 1
            else:
                n_failed += 1
        rep.batch_done(n_done=n_done, n_failed=n_failed, total_elapsed_s=time.time() - t0)


if __name__ == "__main__":
    main()
