"""Sketch: drop ExperimentReporter into a PyTorch/Keras-style callback.

The BaseCallback interface here mirrors DeepTS-Flow's own callback
protocol but the pattern is generic — any training framework with
epoch hooks works.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from argus import ExperimentReporter


class MonitorCallback:
    """Maps training-loop events onto reporter.job_* calls."""

    def __init__(
        self,
        reporter: ExperimentReporter,
        job_id: str,
        model: Optional[str] = None,
        dataset: Optional[str] = None,
    ):
        self.rep = reporter
        self.job_id = job_id
        self.model = model
        self.dataset = dataset
        self._t0 = 0.0

    # ---- typical hooks --------------------------------------------------
    def on_train_begin(self, **_: Any) -> None:
        import time
        self._t0 = time.time()
        self.rep.job_start(job_id=self.job_id, model=self.model, dataset=self.dataset)

    def on_epoch_end(self, epoch: int, logs: Optional[Dict[str, Any]] = None, **_: Any) -> None:
        logs = logs or {}
        self.rep.job_epoch(
            job_id=self.job_id,
            epoch=epoch,
            train_loss=logs.get("train_loss"),
            val_loss=logs.get("val_loss"),
            lr=logs.get("lr"),
        )

    def on_train_end(self, metrics: Optional[Dict[str, Any]] = None, **_: Any) -> None:
        import time
        elapsed = time.time() - self._t0
        self.rep.job_done(
            job_id=self.job_id,
            metrics=metrics,
            elapsed_s=elapsed,
        )

    def on_error(self, reason: str, **_: Any) -> None:
        import time
        self.rep.job_failed(
            job_id=self.job_id, reason=reason, elapsed_s=time.time() - self._t0
        )


if __name__ == "__main__":
    # Tiny synthetic example
    with ExperimentReporter(
        url="http://localhost:9999",
        project="example",
        timeout=0.5,
    ) as rep:
        rep.batch_start(experiment_type="demo", n_total=1)
        cb = MonitorCallback(rep, job_id="demo-1", model="toy", dataset="toy")
        cb.on_train_begin()
        for ep in range(5):
            cb.on_epoch_end(ep, logs={"train_loss": 0.5 - ep * 0.1, "val_loss": 0.6 - ep * 0.1})
        cb.on_train_end(metrics={"MSE": 0.3})
        rep.batch_done(n_done=1, n_failed=0)
