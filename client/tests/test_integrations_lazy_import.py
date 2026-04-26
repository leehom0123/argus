"""Lazy-import contract for ``argus.integrations.*``.

The integration submodules MUST be importable even when their optional
deps (pytorch-lightning, keras, tensorflow) are not installed. The
``ArgusCallback`` class itself only resolves the missing base on first
*instantiation*, so users who merely ``from argus.integrations.keras
import ArgusCallback`` (e.g. for type hints) are unaffected.
"""
from __future__ import annotations

import importlib
import sys

import pytest


def _wipe(*module_prefixes: str):
    """Remove cached modules so their import is re-executed fresh."""
    for name in list(sys.modules.keys()):
        for prefix in module_prefixes:
            if name == prefix or name.startswith(prefix + "."):
                del sys.modules[name]
                break


def test_lightning_import_succeeds_without_pytorch_lightning(monkeypatch):
    """Importing the lightning adapter must not require lightning to be installed."""
    # Block all candidate Lightning import paths.
    monkeypatch.setitem(sys.modules, "pytorch_lightning", None)
    monkeypatch.setitem(sys.modules, "lightning", None)
    _wipe("argus.integrations.lightning")

    mod = importlib.import_module("argus.integrations.lightning")
    assert hasattr(mod, "ArgusCallback")

    # But instantiation should fail with the install hint.
    with pytest.raises(ImportError, match="argus-reporter\\[lightning\\]"):
        mod._Base = None  # ensure we re-resolve, not use a cached stub
        mod.ArgusCallback(project="x", job_id="y")


def test_keras_import_succeeds_without_keras(monkeypatch):
    """Importing the keras adapter must not require keras/tf to be installed."""
    monkeypatch.setitem(sys.modules, "keras", None)
    monkeypatch.setitem(sys.modules, "keras.callbacks", None)
    monkeypatch.setitem(sys.modules, "tensorflow", None)
    monkeypatch.setitem(sys.modules, "tensorflow.keras", None)
    monkeypatch.setitem(sys.modules, "tensorflow.keras.callbacks", None)
    _wipe("argus.integrations.keras")

    mod = importlib.import_module("argus.integrations.keras")
    assert hasattr(mod, "ArgusCallback")

    with pytest.raises(ImportError, match="argus-reporter\\[keras\\]"):
        mod._Base = None
        mod.ArgusCallback(project="x", job_id="y")


def test_hydra_import_succeeds_without_hydra(monkeypatch):
    """Importing the hydra adapter must not require hydra-core to be installed."""
    monkeypatch.setitem(sys.modules, "hydra", None)
    monkeypatch.setitem(sys.modules, "hydra.experimental", None)
    monkeypatch.setitem(sys.modules, "hydra.experimental.callback", None)
    _wipe("argus.integrations.hydra")

    mod = importlib.import_module("argus.integrations.hydra")
    assert hasattr(mod, "ArgusCallback")

    with pytest.raises(ImportError, match="argus-reporter\\[hydra\\]"):
        mod._Base = None
        mod.ArgusCallback(project="x")
