"""Tests for ``argus.integrations.keras.ArgusCallback``.

We stub ``keras`` (the standalone Keras 3 package) into ``sys.modules``
before the adapter resolves its base class. A second test exercises the
``tensorflow.keras`` fallback path.
"""
from __future__ import annotations

import importlib
import sys
import types
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stub Keras into sys.modules.
# ---------------------------------------------------------------------------

def _install_keras_stub(monkeypatch, *, prefer: str = "keras"):
    """Wire either ``keras.callbacks`` or ``tensorflow.keras.callbacks``.

    ``prefer="keras"`` simulates a Keras-3 install (no tensorflow), while
    ``prefer="tensorflow"`` simulates the TF-bundled tf.keras case.
    """

    class _StubCallback:
        """Minimal keras.callbacks.Callback stand-in."""
        # Keras's real Callback initializes ``params`` and ``model`` later via
        # ``set_params`` / ``set_model``; tests set these manually as needed.

    if prefer == "keras":
        keras_mod = types.ModuleType("keras")
        keras_callbacks = types.ModuleType("keras.callbacks")
        keras_callbacks.Callback = _StubCallback
        keras_mod.callbacks = keras_callbacks
        keras_mod.__version__ = "3.0.0"
        monkeypatch.setitem(sys.modules, "keras", keras_mod)
        monkeypatch.setitem(sys.modules, "keras.callbacks", keras_callbacks)
        # Make sure the tf path doesn't accidentally win.
        monkeypatch.setitem(sys.modules, "tensorflow", None)
    else:
        tf_mod = types.ModuleType("tensorflow")
        tf_keras = types.ModuleType("tensorflow.keras")
        tf_keras_cb = types.ModuleType("tensorflow.keras.callbacks")
        tf_keras_cb.Callback = _StubCallback
        tf_keras.callbacks = tf_keras_cb
        tf_mod.keras = tf_keras
        tf_mod.__version__ = "2.15.0"
        monkeypatch.setitem(sys.modules, "tensorflow", tf_mod)
        monkeypatch.setitem(sys.modules, "tensorflow.keras", tf_keras)
        monkeypatch.setitem(sys.modules, "tensorflow.keras.callbacks", tf_keras_cb)
        # Block standalone keras to force fallback.
        monkeypatch.setitem(sys.modules, "keras", None)


@pytest.fixture
def keras_stub(monkeypatch):
    _install_keras_stub(monkeypatch, prefer="keras")
    if "argus.integrations.keras" in sys.modules:
        del sys.modules["argus.integrations.keras"]
    mod = importlib.import_module("argus.integrations.keras")
    mod._Base = None
    return mod


@pytest.fixture
def tf_keras_stub(monkeypatch):
    _install_keras_stub(monkeypatch, prefer="tensorflow")
    if "argus.integrations.keras" in sys.modules:
        del sys.modules["argus.integrations.keras"]
    mod = importlib.import_module("argus.integrations.keras")
    mod._Base = None
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_reporter_stub() -> MagicMock:
    rep = MagicMock(name="Reporter")
    rep.__enter__.return_value = rep
    rep.__exit__.return_value = None

    job = MagicMock(name="JobContext")
    job.__enter__.return_value = job
    job.__exit__.return_value = None
    job._epochs_seen = 0
    rep.job.return_value = job
    rep._job = job
    return rep


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_keras_callback_emits_full_event_sequence(keras_stub):
    rep = _build_reporter_stub()
    cb = keras_stub.ArgusCallback(
        project="proj", job_id="krun-1", _reporter=rep,
    )
    cb.params = {"epochs": 3}

    cb.on_train_begin()
    rep.__enter__.assert_called_once()
    rep.job.assert_called_once_with("krun-1", model=None, dataset=None)
    rep._job.__enter__.assert_called_once()

    cb.on_epoch_end(0, logs={"loss": 0.5, "val_loss": 0.7, "accuracy": 0.92})
    rep._job.epoch.assert_called_once()
    args = rep._job.epoch.call_args.args
    kwargs = rep._job.epoch.call_args.kwargs
    assert args == (0,)
    assert kwargs["train_loss"] == pytest.approx(0.5)
    assert kwargs["val_loss"] == pytest.approx(0.7)
    # extra metrics should be forwarded as kwargs
    assert kwargs["accuracy"] == pytest.approx(0.92)

    cb.on_train_end(logs={"loss": 0.3, "val_loss": 0.4})
    rep._job.__exit__.assert_called_once_with(None, None, None)
    rep.__exit__.assert_called_once_with(None, None, None)


def test_keras_callback_captures_metrics_dict(keras_stub):
    rep = _build_reporter_stub()
    cb = keras_stub.ArgusCallback(
        project="proj",
        job_id="krun-2",
        final_metric_keys=("val_loss", "accuracy"),
        _reporter=rep,
    )
    cb.params = {"epochs": 2}

    cb.on_train_begin()
    cb.on_epoch_end(0, logs={"loss": 0.4, "val_loss": 0.5, "accuracy": 0.88})
    cb.on_train_end()  # no logs -> falls back to last epoch's logs

    rep._job.metrics.assert_called_once()
    payload = rep._job.metrics.call_args.args[0]
    assert payload == {
        "val_loss": pytest.approx(0.5),
        "accuracy": pytest.approx(0.88),
    }


def test_keras_callback_report_failure_marks_job_failed(keras_stub):
    """report_failure is the user-facing replacement for on_exception."""
    rep = _build_reporter_stub()
    cb = keras_stub.ArgusCallback(
        project="proj", job_id="krun-3", _reporter=rep,
    )
    cb.params = {"epochs": 1}

    cb.on_train_begin()
    boom = ValueError("nan loss")
    cb.report_failure(boom)

    job_exit = rep._job.__exit__.call_args.args
    assert job_exit[0] is ValueError
    assert job_exit[1] is boom
    rep_exit = rep.__exit__.call_args.args
    assert rep_exit[0] is ValueError
    assert rep_exit[1] is boom

    # Idempotent: a subsequent on_train_end after report_failure is a no-op.
    rep._job.__exit__.reset_mock()
    rep.__exit__.reset_mock()
    cb.on_train_end()
    rep._job.__exit__.assert_not_called()
    rep.__exit__.assert_not_called()


def test_keras_callback_falls_back_to_tf_keras(tf_keras_stub):
    """When standalone keras is missing, ``tensorflow.keras`` is used."""
    import tensorflow as tf  # the stub

    cb = tf_keras_stub.ArgusCallback(project="proj", job_id="krun-4")
    assert isinstance(cb, tf.keras.callbacks.Callback)


def test_keras_callback_isinstance_of_keras_callback(keras_stub):
    import keras  # the stub

    cb = keras_stub.ArgusCallback(project="proj", job_id="krun-5")
    assert isinstance(cb, keras.callbacks.Callback)


def test_keras_repr_masks_token(keras_stub):
    rep = _build_reporter_stub()
    cb = keras_stub.ArgusCallback(
        project="p", job_id="j", argus_url="x",
        token="secret123", _reporter=rep,
    )
    assert "secret123" not in repr(cb)
    assert "<redacted>" in repr(cb)


def test_keras_pickle_blocked(keras_stub):
    import pickle

    rep = _build_reporter_stub()
    cb = keras_stub.ArgusCallback(
        project="p", job_id="j", argus_url="x",
        token="secret123", _reporter=rep,
    )
    with pytest.raises(TypeError):
        pickle.dumps(cb)
