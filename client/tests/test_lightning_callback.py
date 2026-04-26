"""Tests for ``argus.integrations.lightning.ArgusCallback``.

These tests do not require PyTorch Lightning to be installed. We inject a
minimal stub ``pytorch_lightning`` module into ``sys.modules`` before the
adapter resolves its base class — that's the same pattern Lightning's own
test suite uses for cross-version compat. The stub exposes
``Callback`` with a no-op constructor; the adapter uses ``__new__`` to
inject this stub as a real base class so ``isinstance`` checks pass.
"""
from __future__ import annotations

import importlib
import sys
import types
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stub Lightning into sys.modules before the adapter is imported.
# ---------------------------------------------------------------------------

@pytest.fixture
def lightning_stub(monkeypatch):
    """Install a minimal ``pytorch_lightning`` stub for the duration of the test."""

    class _StubCallback:
        """Minimal pl.Callback stand-in: empty hooks, no-arg ctor."""

    pl = types.ModuleType("pytorch_lightning")
    pl.Callback = _StubCallback
    pl.__version__ = "2.1.0"

    monkeypatch.setitem(sys.modules, "pytorch_lightning", pl)
    # Also ensure ``lightning.pytorch`` resolution doesn't accidentally win.
    monkeypatch.setitem(sys.modules, "lightning", None)

    # Force re-import of our module so ``_resolve_lightning_callback_base``
    # picks up the stub.
    if "argus.integrations.lightning" in sys.modules:
        del sys.modules["argus.integrations.lightning"]
    mod = importlib.import_module("argus.integrations.lightning")
    # Reset the cached base so each test exercises resolution fresh.
    mod._Base = None
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_reporter_stub() -> MagicMock:
    """Mock ``Reporter`` whose ``job()`` returns a mock JobContext."""
    rep = MagicMock(name="Reporter")
    rep.__enter__.return_value = rep
    rep.__exit__.return_value = None

    job = MagicMock(name="JobContext")
    job.__enter__.return_value = job
    job.__exit__.return_value = None
    job._epochs_seen = 0  # adapter peeks at this
    rep.job.return_value = job
    rep._job = job  # convenience handle for tests
    return rep


def _make_trainer(callback_metrics: Dict[str, Any], *, epoch: int = 0,
                  max_epochs: int = 5, sanity: bool = False, lr: float = 1e-3):
    trainer = MagicMock(name="Trainer")
    trainer.callback_metrics = callback_metrics
    trainer.current_epoch = epoch
    trainer.max_epochs = max_epochs
    trainer.sanity_checking = sanity
    opt = MagicMock(name="Optimizer")
    opt.param_groups = [{"lr": lr}]
    trainer.optimizers = [opt]
    return trainer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_lightning_callback_emits_full_event_sequence(lightning_stub):
    """on_train_start -> epoch -> on_train_end fires Reporter+Job lifecycle."""
    rep = _build_reporter_stub()
    cb = lightning_stub.ArgusCallback(
        project="proj", job_id="run-1", _reporter=rep,
    )

    trainer = _make_trainer({"train_loss_epoch": 0.5, "val_loss": 0.7})
    cb.on_train_start(trainer, MagicMock())

    rep.__enter__.assert_called_once()
    rep.job.assert_called_once_with("run-1", model=None, dataset=None)
    rep._job.__enter__.assert_called_once()

    cb.on_train_epoch_end(trainer, MagicMock())
    rep._job.epoch.assert_called_once()
    kwargs = rep._job.epoch.call_args.kwargs
    args = rep._job.epoch.call_args.args
    assert args == (0,)
    assert kwargs["train_loss"] == pytest.approx(0.5)
    assert kwargs["val_loss"] == pytest.approx(0.7)
    assert kwargs["lr"] == pytest.approx(1e-3)

    cb.on_train_end(trainer, MagicMock())
    rep._job.__exit__.assert_called_once_with(None, None, None)
    rep.__exit__.assert_called_once_with(None, None, None)


def test_lightning_callback_captures_metrics_dict(lightning_stub):
    """final_metric_keys should be stashed via JobContext.metrics()."""
    rep = _build_reporter_stub()
    cb = lightning_stub.ArgusCallback(
        project="proj",
        job_id="run-2",
        final_metric_keys=("MSE", "MAE"),
        _reporter=rep,
    )

    trainer = _make_trainer({"MSE": 0.21, "MAE": 0.13, "noise": 999.0})
    cb.on_train_start(trainer, MagicMock())
    cb.on_train_end(trainer, MagicMock())

    rep._job.metrics.assert_called_once()
    payload = rep._job.metrics.call_args.args[0]
    assert payload == {"MSE": pytest.approx(0.21), "MAE": pytest.approx(0.13)}


def test_lightning_callback_on_exception_marks_failure(lightning_stub):
    """on_exception -> JobContext + Reporter exit with exc_info propagated."""
    rep = _build_reporter_stub()
    cb = lightning_stub.ArgusCallback(
        project="proj", job_id="run-3", _reporter=rep,
    )

    trainer = _make_trainer({"train_loss_epoch": 0.4})
    cb.on_train_start(trainer, MagicMock())

    boom = RuntimeError("bad batch")
    cb.on_exception(trainer, MagicMock(), boom)

    job_exit = rep._job.__exit__.call_args.args
    assert job_exit[0] is RuntimeError
    assert job_exit[1] is boom

    rep_exit = rep.__exit__.call_args.args
    assert rep_exit[0] is RuntimeError
    assert rep_exit[1] is boom


def test_lightning_callback_skips_sanity_validation(lightning_stub):
    """on_validation_epoch_end during pre-train sanity check must not emit."""
    rep = _build_reporter_stub()
    cb = lightning_stub.ArgusCallback(
        project="proj", job_id="run-4", _reporter=rep,
    )

    trainer = _make_trainer({"val_loss": 0.9}, sanity=True)
    cb.on_train_start(trainer, MagicMock())
    cb.on_validation_epoch_end(trainer, MagicMock())

    rep._job.epoch.assert_not_called()


def test_lightning_callback_isinstance_of_pl_callback(lightning_stub):
    """The instance must satisfy ``isinstance(cb, pl.Callback)`` or Lightning
    will reject it as not-a-callback."""
    import pytorch_lightning as pl  # the stub from the fixture

    cb = lightning_stub.ArgusCallback(project="proj", job_id="run-5")
    assert isinstance(cb, pl.Callback)


def test_lightning_emits_one_event_when_both_train_and_val_fire(lightning_stub):
    """PL 2.x fires on_validation_epoch_end BEFORE on_train_epoch_end. Must not emit twice."""
    rep = _build_reporter_stub()
    cb = lightning_stub.ArgusCallback(
        project="p", job_id="j", _reporter=rep,
    )

    trainer = _make_trainer(
        {"train_loss_epoch": 0.5, "val_loss": 0.6}, epoch=0,
    )
    cb.on_train_start(trainer, MagicMock())

    # PL 2.x ordering: validation fires first.
    cb.on_validation_epoch_end(trainer, MagicMock())
    cb.on_train_epoch_end(trainer, MagicMock())

    # Exactly one job_epoch event for this epoch.
    assert rep._job.epoch.call_count == 1


def test_lightning_emits_once_for_legacy_train_then_val_order(lightning_stub):
    """PL 1.x ordering (train -> val) must also produce a single event."""
    rep = _build_reporter_stub()
    cb = lightning_stub.ArgusCallback(
        project="p", job_id="j2", _reporter=rep,
    )

    trainer = _make_trainer(
        {"train_loss_epoch": 0.5, "val_loss": 0.6}, epoch=0,
    )
    cb.on_train_start(trainer, MagicMock())

    cb.on_train_epoch_end(trainer, MagicMock())
    cb.on_validation_epoch_end(trainer, MagicMock())

    assert rep._job.epoch.call_count == 1


def test_lightning_repr_masks_token(lightning_stub):
    rep = _build_reporter_stub()
    cb = lightning_stub.ArgusCallback(
        project="p", job_id="j", argus_url="x",
        token="secret123", _reporter=rep,
    )
    assert "secret123" not in repr(cb)
    assert "<redacted>" in repr(cb)


def test_lightning_pickle_blocked(lightning_stub):
    import pickle

    rep = _build_reporter_stub()
    cb = lightning_stub.ArgusCallback(
        project="p", job_id="j", argus_url="x",
        token="secret123", _reporter=rep,
    )
    with pytest.raises(TypeError):
        pickle.dumps(cb)
