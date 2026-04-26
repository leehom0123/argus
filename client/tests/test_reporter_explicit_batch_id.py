"""SDK ``Reporter`` accepts caller-supplied / derived batch ids.

The crash-resume workflow added in v0.2.1 needs the high-level
:class:`argus.Reporter` context manager to honour an explicit id (or a
deterministic one derived from project + experiment + commit) instead of
always minting a fresh UUID. Re-running the same launcher then lands on
the same Batch row on the backend (which is idempotent on ``batch_id``).

Coverage:
* ``batch_id="..."`` overrides the auto-generated id and threads through
  ``batch_start`` / job events.
* ``resume_from="..."`` is an alias for ``batch_id`` so callers can
  signal intent in their code.
* ``derive_batch_id`` is deterministic in the (project, experiment,
  git_sha) triple.
* Default behaviour (no override, no derivation) still yields a fresh
  ``<prefix>-<12 hex>`` UUID — the historical contract is preserved.
* A reused id round-trips through every emitted event so the backend
  can co-locate them under one batch.
"""
from __future__ import annotations

import time

import pytest

from argus import Reporter, derive_batch_id, new_batch_id


def _wait_for(received, n, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(received) >= n:
            return True
        time.sleep(0.05)
    return False


def test_explicit_batch_id_overrides_default(reporter_url, received_events,
                                              monkeypatch):
    """``batch_id="..."`` short-circuits the UUID generator."""
    monkeypatch.setenv("ARGUS_TOKEN", "em_live_test_token")
    custom = "bench-fixed-1234567890ab"
    with Reporter(
        batch_prefix="bench",
        experiment_type="forecast",
        source_project="test-project",
        n_total=1,
        heartbeat=False,
        stop_polling=False,
        resource_snapshot=False,
        monitor_url=reporter_url,
        token="em_live_test_token",
        batch_id=custom,
    ) as r:
        assert r.batch_id == custom
        with r.job("j1", model="m", dataset="d") as j:
            j.metrics({"MSE": 0.5})
    assert _wait_for(received_events, 4)
    for ev in received_events:
        assert ev["batch_id"] == custom


def test_resume_from_aliases_batch_id(reporter_url, received_events):
    """``resume_from="..."`` is an alias of ``batch_id``."""
    rid = "bench-resume-abcdef000001"
    with Reporter(
        experiment_type="forecast",
        source_project="test-project",
        n_total=1,
        heartbeat=False,
        stop_polling=False,
        resource_snapshot=False,
        monitor_url=reporter_url,
        token="em_live_test_token",
        resume_from=rid,
    ) as r:
        assert r.batch_id == rid
        with r.job("jR", model="m", dataset="d"):
            pass
    assert _wait_for(received_events, 3)
    for ev in received_events:
        assert ev["batch_id"] == rid


def test_default_batch_id_still_uuid(reporter_url, received_events):
    """No override → fall back to the historical ``<prefix>-<12 hex>`` UUID."""
    with Reporter(
        batch_prefix="batch",
        experiment_type="forecast",
        source_project="test-project",
        n_total=0,
        heartbeat=False,
        stop_polling=False,
        resource_snapshot=False,
        monitor_url=reporter_url,
        token="em_live_test_token",
    ) as r:
        assert r.batch_id.startswith("batch-")
        # 12 hex chars after the prefix per :func:`new_batch_id`.
        assert len(r.batch_id.split("-", 1)[1]) == 12


def test_explicit_batch_id_takes_precedence_over_resume_from(reporter_url,
                                                              received_events):
    """If a caller passes both, ``batch_id`` wins (more specific arg)."""
    with Reporter(
        experiment_type="forecast",
        source_project="test-project",
        n_total=0,
        heartbeat=False,
        stop_polling=False,
        resource_snapshot=False,
        monitor_url=reporter_url,
        token="em_live_test_token",
        batch_id="bench-explicit",
        resume_from="bench-resume",
    ) as r:
        assert r.batch_id == "bench-explicit"


def test_derive_batch_id_deterministic():
    """Same triple → same id; different triples → different ids."""
    a = derive_batch_id("sibyl", "etth1_transformer", git_sha="deadbeef")
    b = derive_batch_id("sibyl", "etth1_transformer", git_sha="deadbeef")
    assert a == b
    # Format: <prefix>-<16 hex>.
    prefix, _, suffix = a.partition("-")
    assert prefix == "bench"
    assert len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in suffix)

    # Different commit → different id.
    c = derive_batch_id("sibyl", "etth1_transformer", git_sha="cafef00d")
    assert c != a

    # Different project → different id.
    d = derive_batch_id("other", "etth1_transformer", git_sha="deadbeef")
    assert d != a

    # Custom prefix.
    e = derive_batch_id("sibyl", "etth1_transformer", git_sha="deadbeef",
                        prefix="resume")
    assert e.startswith("resume-")


def test_derive_batch_id_handles_missing_git():
    """Empty / missing git sha falls back to a stable ``"no-git"`` token."""
    a = derive_batch_id("sibyl", "exp", git_sha="")
    # Internal contract: empty string → treated as "no-git" sentinel.
    from argus.identity import _NO_GIT, derive_batch_id as _derive
    b = _derive("sibyl", "exp", git_sha=_NO_GIT)
    assert a == b
