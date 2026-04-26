"""Tests for the async email worker (Team Email / BE-2)."""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_enqueue_and_send_success():
    from backend.services import email_worker as w
    from backend.services.email import get_email_service

    w.reset_metrics_for_tests()
    job = w.EmailJob(
        to="user@example.com", subject="hi", body_html="<p>hi</p>",
        body_text="hi", event_type="job_finished",
    )
    w.enqueue(job)
    await w._process_job(job, get_email_service())
    m = w.get_metrics()
    assert m["sent_last_hour"] == 1
    assert m["failed_last_hour"] == 0
    assert m["deadletter_count"] == 0


@pytest.mark.asyncio
async def test_retry_then_success(monkeypatch):
    from backend.services import email_worker as w

    w.reset_metrics_for_tests()
    monkeypatch.setattr(w, "_RETRY_DELAYS", (0.0, 0.0, 0.0))
    calls = {"n": 0}

    async def flaky(job, svc):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(w, "_send_one", flaky)
    w.start_worker()
    try:
        w.enqueue(w.EmailJob(to="u@example.com", subject="s", body_html="b", event_type="test"))
        await asyncio.sleep(0.2)
    finally:
        await w.stop_worker(timeout=1.0)

    assert calls["n"] == 2
    m = w.get_metrics()
    assert m["sent_last_hour"] == 1
    assert m["failed_last_hour"] == 1


@pytest.mark.asyncio
async def test_dead_letter_after_max_attempts(monkeypatch):
    from backend.services import email_worker as w

    w.reset_metrics_for_tests()
    monkeypatch.setattr(w, "_RETRY_DELAYS", (0.0, 0.0, 0.0))
    calls = {"n": 0}

    async def fail(job, svc):
        calls["n"] += 1
        return False

    monkeypatch.setattr(w, "_send_one", fail)
    captured = {}

    async def fake_dl(job, err):
        captured["job"] = job
        w._METRICS["deadletter_count"] = int(w._METRICS["deadletter_count"]) + 1

    monkeypatch.setattr(w, "_write_dead_letter", fake_dl)

    w.start_worker()
    try:
        w.enqueue(w.EmailJob(
            to="u@example.com", subject="s", body_html="b",
            event_type="test", payload={"marker": 42},
        ))
        await asyncio.sleep(0.3)
    finally:
        await w.stop_worker(timeout=1.0)

    assert calls["n"] == 3
    assert captured["job"].attempts == 3
    assert captured["job"].payload == {"marker": 42}
    assert w.get_metrics()["deadletter_count"] == 1


def test_retry_delay_schedule():
    from backend.services import email_worker as w

    assert w._RETRY_DELAYS == (1.0, 5.0, 30.0)
    assert w._MAX_ATTEMPTS == 3


@pytest.mark.asyncio
async def test_stop_worker_is_idempotent():
    from backend.services import email_worker as w

    w.reset_metrics_for_tests()
    await w.stop_worker(timeout=0.1)
    assert w.start_worker() is not None
    await w.stop_worker(timeout=1.0)
    await w.stop_worker(timeout=0.1)


@pytest.mark.asyncio
async def test_get_metrics_prunes_old_timestamps():
    from backend.services import email_worker as w
    import time

    w.reset_metrics_for_tests()
    w._METRICS["sent_times"].append(time.monotonic() - 4000)
    w._METRICS["sent_times"].append(time.monotonic())
    assert w.get_metrics()["sent_last_hour"] == 1
