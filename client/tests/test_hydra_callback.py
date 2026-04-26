"""Tests for ``argus.integrations.hydra.ArgusCallback``.

These tests do not require hydra-core to be installed. We inject a
minimal stub ``hydra.experimental.callback`` module into ``sys.modules``
before the adapter resolves its base class — same pattern as the
Lightning / Keras suites.
"""
from __future__ import annotations

import importlib
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stub Hydra into sys.modules before the adapter is imported.
# ---------------------------------------------------------------------------

@pytest.fixture
def hydra_stub(monkeypatch):
    """Install a minimal ``hydra`` stub for the duration of the test."""

    class _StubCallback:
        """Minimal hydra Callback stand-in: empty hooks, no-arg ctor."""

    hydra = types.ModuleType("hydra")
    hydra_exp = types.ModuleType("hydra.experimental")
    hydra_cb = types.ModuleType("hydra.experimental.callback")
    hydra_cb.Callback = _StubCallback

    # HydraConfig namespace (used for job.num lookup).
    hydra_core = types.ModuleType("hydra.core")
    hydra_core_hc = types.ModuleType("hydra.core.hydra_config")

    class _StubJob:
        num = 0

    class _StubHydraCfg:
        job = _StubJob()

    class _StubHydraConfig:
        @staticmethod
        def get():
            return _StubHydraCfg()

    hydra_core_hc.HydraConfig = _StubHydraConfig

    monkeypatch.setitem(sys.modules, "hydra", hydra)
    monkeypatch.setitem(sys.modules, "hydra.experimental", hydra_exp)
    monkeypatch.setitem(sys.modules, "hydra.experimental.callback", hydra_cb)
    monkeypatch.setitem(sys.modules, "hydra.core", hydra_core)
    monkeypatch.setitem(sys.modules, "hydra.core.hydra_config", hydra_core_hc)

    if "argus.integrations.hydra" in sys.modules:
        del sys.modules["argus.integrations.hydra"]
    mod = importlib.import_module("argus.integrations.hydra")
    mod._Base = None
    return mod, _StubHydraCfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_reporter_stub() -> MagicMock:
    """Mock ``Reporter`` whose ``job()`` returns a mock JobContext."""
    rep = MagicMock(name="Reporter")
    rep.__enter__.return_value = rep
    rep.__exit__.return_value = None
    rep.batch_id = "stub-batch-abc123"

    job = MagicMock(name="JobContext")
    job.__enter__.return_value = job
    job.__exit__.return_value = None
    rep.job.return_value = job
    rep._job = job  # convenience handle for tests
    return rep


def _make_job_return(*, status: str = "COMPLETED", value: Any = None):
    """Mock a Hydra ``JobReturn``."""
    jr = MagicMock(name="JobReturn")
    jr.status = MagicMock()
    jr.status.name = status
    jr._return_value = value
    jr.return_value = value
    return jr


def _config(**fields):
    """Build a simple object that quacks like DictConfig."""
    cfg = types.SimpleNamespace(**fields)
    return cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_hydra_callback_isinstance_of_hydra_callback(hydra_stub):
    """ArgusCallback must satisfy isinstance(cb, hydra Callback) after the __new__ shim."""
    mod, _ = hydra_stub
    from hydra.experimental.callback import Callback  # the stub

    cb = mod.ArgusCallback(project="proj")
    assert isinstance(cb, Callback)


def test_single_run_emits_one_batch_one_job(hydra_stub):
    """on_run_start -> on_job_start -> on_job_end -> on_run_end fires lifecycle in order."""
    mod, stub_cfg = hydra_stub
    rep = _build_reporter_stub()
    cb = mod.ArgusCallback(project="proj", _reporter=rep)

    config = _config(experiment_name="exp1")

    cb.on_run_start(config)
    rep.__enter__.assert_called_once()
    rep.job.assert_not_called()

    cb.on_job_start(config)
    rep.job.assert_called_once()
    rep._job.__enter__.assert_called_once()
    # job_id defaults to HydraConfig.job.num (stub returns 0).
    assert rep.job.call_args.args[0] == "0"

    cb.on_job_end(config, _make_job_return(status="COMPLETED"))
    rep._job.__exit__.assert_called_once_with(None, None, None)

    cb.on_run_end(config)
    rep.__exit__.assert_called_once_with(None, None, None)


def test_multirun_emits_one_batch_n_jobs(hydra_stub):
    """on_multirun_start opens 1 batch; each (on_job_start/end) opens+closes a job."""
    mod, stub_cfg = hydra_stub
    rep = _build_reporter_stub()
    cb = mod.ArgusCallback(project="proj", _reporter=rep)

    config = _config(experiment_name="sweep")
    cb.on_multirun_start(config)
    rep.__enter__.assert_called_once()

    # 3 trials, each with a distinct HydraConfig.job.num.
    for trial_num in (0, 1, 2):
        stub_cfg.job.num = trial_num
        cb.on_job_start(config)
        cb.on_job_end(config, _make_job_return(status="COMPLETED"))

    assert rep.job.call_count == 3
    job_ids = [c.args[0] for c in rep.job.call_args_list]
    assert job_ids == ["0", "1", "2"]
    # All three job exits clean.
    assert rep._job.__exit__.call_count == 3
    for call in rep._job.__exit__.call_args_list:
        assert call.args == (None, None, None)

    # on_run_end during multirun must NOT close the reporter.
    cb.on_run_end(config)
    rep.__exit__.assert_not_called()

    cb.on_multirun_end(config)
    rep.__exit__.assert_called_once_with(None, None, None)


def test_failed_job_propagates_exc_to_jobcontext(hydra_stub):
    """JobReturn(status=FAILED) -> JobContext.__exit__ called with exc_info."""
    mod, _ = hydra_stub
    rep = _build_reporter_stub()
    cb = mod.ArgusCallback(project="p", _reporter=rep)

    cb.on_run_start(_config())
    cb.on_job_start(_config())

    boom = ValueError("training diverged")
    cb.on_job_end(_config(), _make_job_return(status="FAILED", value=boom))

    job_exit = rep._job.__exit__.call_args.args
    assert job_exit[0] is ValueError
    assert job_exit[1] is boom

    cb.on_run_end(_config())


def test_job_id_falls_back_to_hydra_config_num(hydra_stub):
    """HydraConfig.job.num is the default job_id when no key/template is set."""
    mod, stub_cfg = hydra_stub
    rep = _build_reporter_stub()
    cb = mod.ArgusCallback(project="p", _reporter=rep)

    stub_cfg.job.num = 7
    cb.on_run_start(_config())
    cb.on_job_start(_config())
    assert rep.job.call_args.args[0] == "7"


def test_job_id_template_uses_config_fields(hydra_stub):
    """job_id_template formats with HydraConfig.job.num + top-level config fields."""
    mod, stub_cfg = hydra_stub
    rep = _build_reporter_stub()
    cb = mod.ArgusCallback(
        project="p",
        job_id_template="{experiment_name}-{job_num}",
        _reporter=rep,
    )

    stub_cfg.job.num = 4
    cfg = _config(experiment_name="dam_forecast")
    cb.on_run_start(cfg)
    cb.on_job_start(cfg)
    assert rep.job.call_args.args[0] == "dam_forecast-4"


def test_batch_prefix_defaults_to_experiment_name(hydra_stub, monkeypatch):
    """When batch_prefix is unset, config.experiment_name is used."""
    mod, _ = hydra_stub

    captured = {}

    class _StubReporter:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.batch_id = "x-1"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def job(self, *a, **kw):
            j = MagicMock()
            j.__enter__.return_value = j
            j.__exit__.return_value = None
            return j

    monkeypatch.setattr(mod, "Reporter", _StubReporter)
    cb = mod.ArgusCallback(project="proj")
    cb.on_run_start(_config(experiment_name="my_exp"))
    assert captured["batch_prefix"] == "my_exp"


def test_argus_url_and_token_fall_back_to_env(hydra_stub, monkeypatch):
    """When constructor args are omitted, env ARGUS_URL / ARGUS_TOKEN propagate."""
    mod, _ = hydra_stub

    captured = {}

    class _StubReporter:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.batch_id = "x-1"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def job(self, *a, **kw):
            j = MagicMock()
            j.__enter__.return_value = j
            j.__exit__.return_value = None
            return j

    monkeypatch.setattr(mod, "Reporter", _StubReporter)
    monkeypatch.setenv("ARGUS_URL", "http://argus.test")
    monkeypatch.setenv("ARGUS_TOKEN", "em_live_test")
    cb = mod.ArgusCallback(project="proj")
    cb.on_run_start(_config())
    # The Reporter ctor receives the explicit None; Reporter itself
    # resolves env in _resolve_monitor_url / _resolve_token. The
    # adapter's contract is "pass through, don't shadow env".
    assert captured["monitor_url"] is None
    assert captured["token"] is None


def test_repr_masks_token(hydra_stub):
    mod, _ = hydra_stub
    cb = mod.ArgusCallback(project="p", token="secret-token-xyz")
    r = repr(cb)
    assert "secret-token-xyz" not in r
    assert "<redacted>" in r


def test_pickle_blocked(hydra_stub):
    import pickle

    mod, _ = hydra_stub
    cb = mod.ArgusCallback(project="p", token="secret-token-xyz")
    with pytest.raises(TypeError):
        pickle.dumps(cb)


def test_set_batch_id_is_called_on_open(hydra_stub, monkeypatch):
    """The reporter's batch_id must be propagated to the global slot."""
    mod, _ = hydra_stub
    rep = _build_reporter_stub()
    rep.batch_id = "hydra-batch-deadbeef"

    seen = {}

    def _capture(bid):
        seen["bid"] = bid

    monkeypatch.setattr(mod, "set_batch_id", _capture)

    cb = mod.ArgusCallback(project="p", _reporter=rep)
    cb.on_run_start(_config())
    assert seen["bid"] == "hydra-batch-deadbeef"


def test_import_error_when_hydra_not_installed(monkeypatch):
    """Without hydra-core, instantiating ArgusCallback must raise ImportError."""
    monkeypatch.setitem(sys.modules, "hydra", None)
    monkeypatch.setitem(sys.modules, "hydra.experimental", None)
    monkeypatch.setitem(sys.modules, "hydra.experimental.callback", None)
    if "argus.integrations.hydra" in sys.modules:
        del sys.modules["argus.integrations.hydra"]

    mod = importlib.import_module("argus.integrations.hydra")
    mod._Base = None
    with pytest.raises(ImportError, match=r"argus-reporter\[hydra\]"):
        mod.ArgusCallback(project="x")
