"""Tests for the context-manager API (Reporter + JobContext).

Most tests use the existing mock HTTP server fixture (`mock_server` from
conftest) which already handles ``/api/events`` and ``/api/events/batch``.
Tests that exercise stop-polling, artifact upload, or want to inspect
direct network calls patch ``requests`` instead — the underlying
:class:`ExperimentReporter` plumbing is covered by the v0.1 test suite.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from argus import (
    ExperimentReporter,
    JobContext,
    Reporter,
    emit,
    new_batch_id,
    set_batch_id,
    sub_env,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def make_reporter(tmp_path):
    """Factory that builds a Reporter wired to the test mock server.
    Daemons default OFF; the underlying ExperimentReporter is closed
    after the test so its worker thread can't deliver stale events to
    the next test's httpserver."""
    created: List[ExperimentReporter] = []

    def _factory(reporter_url: str, **kw) -> Reporter:
        spill = tmp_path / f"spill-{len(created)}.jsonl"
        rep = ExperimentReporter(
            url=reporter_url,
            project="test-project",
            auth_token="em_live_test_token",
            timeout=1.0,
            queue_size=100,
            spill_path=str(spill),
        )
        created.append(rep)
        return Reporter(
            batch_prefix=kw.pop("batch_prefix", "btest"),
            experiment_type=kw.pop("experiment_type", "forecast"),
            source_project=kw.pop("source_project", "test-project"),
            n_total=kw.pop("n_total", 1),
            heartbeat=kw.pop("heartbeat", False),
            stop_polling=kw.pop("stop_polling", False),
            resource_snapshot=kw.pop("resource_snapshot", False),
            monitor_url=reporter_url,
            token="em_live_test_token",
            _reporter=rep,
            **kw,
        )

    yield _factory

    for r in created:
        try:
            r.close(timeout=1.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# basic round-trip + lifecycle events
# ---------------------------------------------------------------------------

def test_reporter_emits_batch_start_and_batch_done(
    mock_server, received_events, make_reporter
):
    url = mock_server.url_for("").rstrip("/")
    with make_reporter(url) as r:
        assert r.batch_id.startswith("btest-")
        assert r.stopped is False

    assert _wait_for(lambda: len(received_events) >= 2)
    types = [ev["event_type"] for ev in received_events]
    assert "batch_start" in types
    assert "batch_done" in types
    # batch_done carries n_done / n_failed / total_elapsed_s
    done = next(ev for ev in received_events if ev["event_type"] == "batch_done")
    assert done["data"]["n_done"] == 0
    assert done["data"]["n_failed"] == 0
    assert "total_elapsed_s" in done["data"]


def test_reporter_emits_batch_failed_on_exception(
    mock_server, received_events, make_reporter
):
    url = mock_server.url_for("").rstrip("/")
    with pytest.raises(RuntimeError, match="boom"):
        with make_reporter(url):
            raise RuntimeError("boom")

    assert _wait_for(
        lambda: any(ev["event_type"] == "batch_failed" for ev in received_events)
    )
    failed = next(ev for ev in received_events if ev["event_type"] == "batch_failed")
    assert "RuntimeError" in failed["data"]["reason"]
    assert "boom" in failed["data"]["reason"]


# ---------------------------------------------------------------------------
# JobContext lifecycle
# ---------------------------------------------------------------------------

def test_job_context_emits_job_start_and_job_done(
    mock_server, received_events, make_reporter
):
    url = mock_server.url_for("").rstrip("/")
    with make_reporter(url) as r:
        with r.job("j1", model="transformer", dataset="etth1") as j:
            assert isinstance(j, JobContext)
            assert j.stopped is False
            j.epoch(0, train_loss=0.5, val_loss=0.6, lr=1e-4)
            j.epoch(1, train_loss=0.4, val_loss=0.5, lr=1e-4)
            j.metrics({"MSE": 0.44, "MAE": 0.42})

    _wait_for(lambda: len(received_events) >= 5)
    types = [ev["event_type"] for ev in received_events]
    assert types.count("job_start") == 1
    assert types.count("job_epoch") == 2
    assert types.count("job_done") == 1
    done = next(ev for ev in received_events if ev["event_type"] == "job_done")
    assert done["job_id"] == "j1"
    assert done["data"]["metrics"] == {"MSE": 0.44, "MAE": 0.42}
    assert done["data"]["train_epochs"] == 2
    epoch_ev = next(ev for ev in received_events if ev["event_type"] == "job_epoch")
    assert epoch_ev["job_id"] == "j1"
    assert epoch_ev["data"]["train_loss"] == 0.5


def test_job_context_emits_job_failed_on_exception(
    mock_server, received_events, make_reporter
):
    url = mock_server.url_for("").rstrip("/")
    with make_reporter(url) as r:
        try:
            with r.job("j1", model="x") as j:
                j.epoch(0, train_loss=0.1)
                raise ValueError("nope")
        except ValueError:
            pass

    _wait_for(
        lambda: any(ev["event_type"] == "job_failed" for ev in received_events)
    )
    failed = next(ev for ev in received_events if ev["event_type"] == "job_failed")
    assert failed["job_id"] == "j1"
    assert "ValueError" in failed["data"]["reason"]


def test_job_log_emits_log_line(mock_server, received_events, make_reporter):
    url = mock_server.url_for("").rstrip("/")
    with make_reporter(url) as r:
        with r.job("j1") as j:
            j.log("training started", level="INFO")

    _wait_for(lambda: any(ev["event_type"] == "log_line" for ev in received_events))
    log = next(ev for ev in received_events if ev["event_type"] == "log_line")
    assert log["data"]["line"] == "training started"
    assert log["data"]["level"] == "info"


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def test_job_upload_posts_each_png(mock_server, tmp_path, make_reporter):
    """j.upload(<dir>) should POST every .png to /api/jobs/<job_id>/artifacts."""
    art = tmp_path / "viz"
    art.mkdir()
    (art / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (art / "b.png").write_bytes(b"\x89PNG\r\n\x1a\nfake2")
    (art / "c.txt").write_text("not an image")

    url = mock_server.url_for("").rstrip("/")
    fake_resp = MagicMock(ok=True, status_code=200, text="{}")

    with patch("argus.context.requests") as mock_req:
        mock_req.post.return_value = fake_resp
        # Stop-poll uses the same module — keep `get` stubbed even though
        # we have stop_polling disabled below.
        mock_req.get.return_value = MagicMock(
            ok=True, json=lambda: {"stop_requested": False}
        )
        with make_reporter(url) as r:
            with r.job("j1", model="m") as j:
                n = j.upload(art)

    assert n == 2
    posted_urls = [c.args[0] for c in mock_req.post.call_args_list]
    assert any("/api/jobs/j1/artifacts" in u for u in posted_urls)
    # No .txt in the posted file names.
    posted_names = []
    for c in mock_req.post.call_args_list:
        files = c.kwargs.get("files") or {}
        if "file" in files:
            posted_names.append(files["file"][0])
    assert sorted(posted_names) == ["a.png", "b.png"]


def test_upload_silent_when_monitor_unreachable(tmp_path):
    """When `requests` raises every time, upload returns 0 cleanly."""
    art = tmp_path / "viz"
    art.mkdir()
    (art / "a.png").write_bytes(b"x")

    rep = ExperimentReporter(
        url="http://127.0.0.1:1",  # unreachable
        project="test-project",
        auth_token="t",
        timeout=0.1,
        queue_size=10,
        spill_path=str(tmp_path / "spill.jsonl"),
    )
    r = Reporter(
        batch_prefix="b",
        heartbeat=False, stop_polling=False, resource_snapshot=False,
        monitor_url="http://127.0.0.1:1", token="t", _reporter=rep,
    )
    try:
        with r as rr:
            with rr.job("j1") as j:
                with patch("argus.context.requests") as mock_req:
                    mock_req.post.side_effect = Exception("connection refused")
                    n = j.upload(art)
        assert n == 0
    finally:
        rep.close(timeout=1.0)


# ---------------------------------------------------------------------------
# daemon-thread lifecycle (no thread leak)
# ---------------------------------------------------------------------------

def test_daemons_start_on_enter_and_stop_on_exit(mock_server, tmp_path):
    url = mock_server.url_for("").rstrip("/")

    pre_threads = {t.ident for t in threading.enumerate()}

    inner = ExperimentReporter(
        url=url, project="test-project", auth_token="t", timeout=1.0,
        spill_path=str(tmp_path / "spill.jsonl"),
    )
    try:
        with patch("argus.context.requests") as mock_req:
            # stop poller: always "not stopped"
            mock_req.get.return_value = MagicMock(
                ok=True, json=lambda: {"stop_requested": False}
            )
            r = Reporter(
                batch_prefix="b",
                experiment_type="forecast",
                source_project="test-project",
                heartbeat=0.05,             # 50ms — fast enough to observe
                stop_polling=0.05,
                resource_snapshot=0.05,
                monitor_url=url,
                token="t",
                _reporter=inner,
            )
            with r:
                inside = {t.name for t in threading.enumerate()}
                assert any("reporter-heartbeat" in n for n in inside)
                assert any("reporter-stop-poll" in n for n in inside)
                assert any("reporter-resource" in n for n in inside)
    finally:
        inner.close(timeout=1.0)

    # Give the daemons a beat to wind down.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        names = [t.name for t in threading.enumerate() if t.is_alive()]
        if not any(
            ("reporter-heartbeat" in n)
            or ("reporter-stop-poll" in n)
            or ("reporter-resource" in n)
            for n in names
        ):
            break
        time.sleep(0.05)
    else:
        leaked = [
            n for n in (t.name for t in threading.enumerate() if t.is_alive())
            if "reporter-" in n
        ]
        pytest.fail(f"daemons did not stop: {leaked}")

    post_threads = {t.ident for t in threading.enumerate()}
    new = post_threads - pre_threads
    # Allow some short-lived lingering threads from pytest-httpserver, but
    # nothing named like our daemons should remain.
    leaks = [
        t for t in threading.enumerate()
        if t.ident in new and "reporter-" in t.name
    ]
    assert leaks == []


# ---------------------------------------------------------------------------
# stop_polling -> r.stopped
# ---------------------------------------------------------------------------

def test_reporter_stopped_flips_when_endpoint_says_so(tmp_path):
    """When the stop-requested endpoint returns true, r.stopped flips."""
    flips: List[Dict[str, Any]] = [
        {"stop_requested": False},
        {"stop_requested": True},
    ]
    flip_lock = threading.Lock()

    def fake_get(url, headers=None, timeout=None):
        with flip_lock:
            payload = flips[0] if len(flips) == 1 else flips.pop(0)
        m = MagicMock()
        m.ok = True
        m.json = lambda p=payload: p
        return m

    with patch("argus.context.requests") as mock_req:
        mock_req.get.side_effect = fake_get
        # Use a fully no-op reporter (no URL) so we don't accidentally
        # post to a real server. Daemons still run; only stop-poll uses
        # real URL (we set one explicitly).
        r = Reporter(
            batch_prefix="b",
            experiment_type="forecast",
            source_project="p",
            heartbeat=False,
            stop_polling=0.05,
            resource_snapshot=False,
            monitor_url="http://example.invalid",
            token="t",
        )
        with r:
            assert _wait_for(lambda: r.stopped is True, timeout=2.0)
            # JobContext inherits .stopped from the parent
            with r.job("j1") as j:
                assert j.stopped is True


# ---------------------------------------------------------------------------
# misc: backward compat + module-level helpers
# ---------------------------------------------------------------------------

def test_backward_compat_imports_still_work():
    # The v0.1.x users do `from argus import ExperimentReporter`.
    # That must keep working alongside the new names.
    from argus import ExperimentReporter as _ER  # noqa: N814
    assert _ER is ExperimentReporter
    # Plus the module-level helpers requested.
    assert callable(emit)
    assert callable(new_batch_id)
    assert callable(set_batch_id)
    assert callable(sub_env)


def test_new_batch_id_format():
    a = new_batch_id()
    b = new_batch_id("run")
    assert a.startswith("batch-") and len(a) == len("batch-") + 12
    assert b.startswith("run-") and len(b) == len("run-") + 12
    assert a != b


def test_set_batch_id_takes_effect_globally(
    mock_server, received_events, make_reporter
):
    url = mock_server.url_for("").rstrip("/")
    with make_reporter(url):
        # set_batch_id mutates the global; verify the property still
        # reads the original (Reporter caches its own batch_id) but the
        # underlying reporter follows the override.
        new_bid = new_batch_id("override")
        set_batch_id(new_bid)
        from argus import get_batch_id
        assert get_batch_id() == new_bid


def test_module_level_emit_no_op_when_no_active_reporter():
    # No Reporter is currently entered.
    emit("log_line", line="hello")  # must not raise


def test_module_level_emit_routes_through_active_reporter(
    mock_server, received_events, make_reporter
):
    url = mock_server.url_for("").rstrip("/")
    with make_reporter(url):
        emit("log_line", line="from-emit", level="info", job_id="j1")
    _wait_for(
        lambda: any(
            ev["event_type"] == "log_line" and ev["data"].get("line") == "from-emit"
            for ev in received_events
        )
    )


def test_sub_env_substitutes_environment_variables(monkeypatch):
    monkeypatch.setenv("MYVAR", "hello")
    assert sub_env("${MYVAR}/world") == "hello/world"
    assert sub_env("$MYVAR/world") == "hello/world"
    # Missing keys are kept as-is.
    monkeypatch.delenv("ABSENT", raising=False)
    assert sub_env("${ABSENT}/x") == "${ABSENT}/x"
    # Explicit overrides win over env.
    assert sub_env("${MYVAR}/x", MYVAR="bye") == "bye/x"


# ---------------------------------------------------------------------------
# silent fallback when ARGUS_URL absent
# ---------------------------------------------------------------------------

def test_reporter_degrades_silently_with_no_monitor_url(monkeypatch, tmp_path):
    monkeypatch.delenv("ARGUS_URL", raising=False)
    monkeypatch.delenv("ARGUS_TOKEN", raising=False)
    # No URL passed in either; should not raise on enter/exit.
    r = Reporter(
        batch_prefix="b",
        experiment_type="forecast",
        source_project="p",
        heartbeat=False,
        stop_polling=False,
        resource_snapshot=False,
    )
    with r:
        with r.job("j1") as j:
            j.epoch(0, train_loss=0.1)
            j.metrics({"MSE": 1.0})
            j.log("hi")
            assert j.upload(tmp_path) == 0


def test_auto_upload_dirs_invoked_on_clean_exit(
    mock_server, tmp_path, make_reporter
):
    art = tmp_path / "viz"
    art.mkdir()
    (art / "a.png").write_bytes(b"x")

    url = mock_server.url_for("").rstrip("/")
    with patch("argus.context.requests") as mock_req:
        mock_req.post.return_value = MagicMock(ok=True, status_code=200, text="{}")
        mock_req.get.return_value = MagicMock(
            ok=True, json=lambda: {"stop_requested": False}
        )
        with make_reporter(url, auto_upload_dirs=[art]):
            pass
    posted_urls = [c.args[0] for c in mock_req.post.call_args_list]
    assert any("/api/batches/" in u and u.endswith("/artifacts") for u in posted_urls)
