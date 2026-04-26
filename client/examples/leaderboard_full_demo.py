"""End-to-end demo: emit every event the leaderboard cares about.

Walks through the canonical sequence:

    batch_start -> job_start -> job_epoch x 10 -> job_done -> batch_done

The ``job_done.metrics`` dict carries a representative mix of error,
latency, compute, and meta keys to show that *any* key your training
emits becomes a leaderboard column. Argus does not validate the
schema — the names below are conventions from the forecasting
workflow, not requirements.

Run::

    export ARGUS_URL=https://argus.example.com
    export ARGUS_TOKEN=em_live_xxxxxxxxxxxxx
    python client/examples/leaderboard_full_demo.py

Then open the ``leaderboard-demo`` project on the dashboard and
refresh the Leaderboard tab.

Use ``--dry-run`` to print the event payloads without contacting any
server (useful for CI, doc screenshots, or smoke-testing schema
changes locally)::

    python client/examples/leaderboard_full_demo.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict


def _final_metrics() -> Dict[str, Any]:
    """One representative metric per category — all user-defined."""
    return {
        # Quality
        "MSE": 0.382,
        "MAE": 0.441,
        "RMSE": 0.618,
        "R2": 0.612,
        # Throughput / latency (seconds)
        "Latency_P50": 0.0142,
        "Latency_P95": 0.0231,
        "Inference_Throughput": 1820.5,
        # Compute footprint (MB / seconds)
        "GPU_Memory": 4321,
        "Total_Train_Time": 612.4,
        "Avg_Epoch_Time": 6.12,
        # Meta
        "Model_Params": 1_234_567,
        "seed": 2021,
    }


def _emit_dry(event: str, **fields: Any) -> None:
    print(f"[dry-run] {event}: {json.dumps(fields, sort_keys=True, default=str)}")


def _run_dry_run() -> int:
    print("=== Argus leaderboard-demo dry run ===")
    _emit_dry("batch_start", experiment_type="forecast",
              n_total=1, command="leaderboard_full_demo.py")
    _emit_dry("job_start", job_id="demo-job-1",
              model="patchtst", dataset="etth1")
    for ep in range(10):
        _emit_dry("job_epoch", job_id="demo-job-1", epoch=ep,
                  train_loss=round(1.0 / (ep + 1), 4),
                  val_loss=round(1.1 / (ep + 1), 4),
                  lr=1e-4)
    _emit_dry("job_done", job_id="demo-job-1",
              metrics=_final_metrics(), elapsed_s=2.0, train_epochs=10)
    _emit_dry("batch_done", n_done=1, n_failed=0, total_elapsed_s=2.0)
    print("=== dry run complete (no HTTP traffic) ===")
    return 0


def _run_live() -> int:
    # Lazy import so --dry-run works even on a machine without the SDK.
    try:
        from argus import Reporter
    except Exception as exc:
        print(f"error: cannot import argus SDK: {exc}", file=sys.stderr)
        print("hint: pip install argus-reporter", file=sys.stderr)
        return 2

    if not os.environ.get("ARGUS_URL"):
        print(
            "error: ARGUS_URL is not set. "
            "Export ARGUS_URL=<your-argus-host> (and ARGUS_TOKEN=<token>) "
            "or rerun with --dry-run.",
            file=sys.stderr,
        )
        return 2

    with Reporter(
        "leaderboard-demo",
        experiment_type="forecast",
        source_project="leaderboard-demo",
        n_total=1,
        command="python client/examples/leaderboard_full_demo.py",
    ) as r:
        with r.job("demo-job-1", model="patchtst", dataset="etth1") as j:
            for ep in range(10):
                j.epoch(
                    ep,
                    train_loss=1.0 / (ep + 1),
                    val_loss=1.1 / (ep + 1),
                    lr=1e-4,
                )
                time.sleep(0.05)
            j.metrics(_final_metrics())
    print("done -- check the leaderboard-demo project on your Argus instance")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print events to stdout instead of POSTing to a server.",
    )
    args = parser.parse_args()
    return _run_dry_run() if args.dry_run else _run_live()


if __name__ == "__main__":
    sys.exit(main())
