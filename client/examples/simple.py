"""Minimal usage example.

Run:
    python examples/simple.py --url http://localhost:9999

The reporter is fire-and-forget: if the server is down, every call still
returns silently (events are spilled to ~/.argus-reporter/spill-*.jsonl
for later retry). Nothing will crash your experiment.
"""
from __future__ import annotations

import argparse
import logging
import time

from argus import ExperimentReporter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:9999")
    parser.add_argument("--project", default="example-project")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    with ExperimentReporter(url=args.url, project=args.project, timeout=0.5) as rep:
        batch_id = rep.batch_start(
            experiment_type="demo",
            n_total=1,
            command="python examples/simple.py",
        )
        print(f"started batch {batch_id}")

        rep.job_start(job_id="demo-job-1", model="toy", dataset="toy")
        for epoch in range(3):
            time.sleep(0.1)
            rep.job_epoch(
                job_id="demo-job-1",
                epoch=epoch,
                train_loss=1.0 / (epoch + 1),
                val_loss=1.1 / (epoch + 1),
                lr=1e-3,
            )
        rep.job_done(
            job_id="demo-job-1",
            metrics={"MSE": 0.33, "MAE": 0.41},
            elapsed_s=0.3,
            train_epochs=3,
        )
        rep.batch_done(n_done=1, n_failed=0, total_elapsed_s=0.3)

    print("done — check the monitor UI (or spill file if server unreachable)")


if __name__ == "__main__":
    main()
