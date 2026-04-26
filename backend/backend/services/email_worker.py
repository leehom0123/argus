"""Async email worker: queued + retry + dead-letter.

Design
------
* Fire-and-forget enqueue; dispatcher doesn't block HTTP on SMTP.
* Single-concurrency worker draining an asyncio.Queue.
* Retry at 1 s -> 5 s -> 30 s, three attempts, then dead-letter via
  re-enqueue after asyncio.sleep.
* Dead-letter writes into email_dead_letter when BE-1's model is
  available; else logs a structured fallback line.
* In-process metrics (queue depth + rolling 1-hour sent/failed +
  cumulative deadletter count). Exposed via /api/admin/email/stats.

Lifespan wiring (app.py): BE-1 adds in lifespan():

    from backend.services.email_worker import start_worker, stop_worker
    email_worker_task = start_worker() if os.environ.get(
        "ARGUS_EMAIL_WORKER_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no") else None
    try:
        yield
    finally:
        if email_worker_task is not None:
            await stop_worker(timeout=2.0)
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from backend.services.email import EmailService, get_email_service

log = logging.getLogger(__name__)


@dataclass
class EmailJob:
    to: str
    subject: str
    body_html: str
    body_text: str = ""
    event_type: str = ""
    payload: dict = field(default_factory=dict)
    attempts: int = 0


_queue = None
_worker_task = None
_RETRY_DELAYS = (1.0, 5.0, 30.0)
_MAX_ATTEMPTS = len(_RETRY_DELAYS)
_METRICS = {
    "sent_times": deque(),
    "failed_times": deque(),
    "deadletter_count": 0,
}


def _prune_older_than(dq, cutoff):
    while dq and dq[0] < cutoff:
        dq.popleft()


def _get_queue():
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def enqueue(job: EmailJob) -> None:
    """Push a job onto the queue."""
    q = _get_queue()
    q.put_nowait(job)
    log.info(
        "email.enqueued to=%s event_type=%s attempts=%d",
        job.to, job.event_type, job.attempts,
    )


def get_metrics():
    now = time.monotonic()
    cutoff = now - 3600
    _prune_older_than(_METRICS["sent_times"], cutoff)
    _prune_older_than(_METRICS["failed_times"], cutoff)
    return {
        "queued": _get_queue().qsize() if _queue is not None else 0,
        "sent_last_hour": len(_METRICS["sent_times"]),
        "failed_last_hour": len(_METRICS["failed_times"]),
        "deadletter_count": int(_METRICS["deadletter_count"]),
    }


def reset_metrics_for_tests():
    global _queue, _worker_task
    _queue = None
    _worker_task = None
    _METRICS["sent_times"].clear()
    _METRICS["failed_times"].clear()
    _METRICS["deadletter_count"] = 0


async def _send_one(job, email_service):
    sender = getattr(email_service, "send_custom", None)
    if sender is None:
        return await _fallback_send_custom(email_service, job)
    return await sender(
        to=job.to,
        subject=job.subject,
        body_html=job.body_html,
        body_text=job.body_text,
    )


async def _fallback_send_custom(email_service, job):
    from email.message import EmailMessage

    settings = email_service._settings  # noqa: SLF001
    if not settings.smtp_configured:
        log.info(
            "[email-dev-stdout] to=%s subject=%r event_type=%s\n%s",
            job.to, job.subject, job.event_type, job.body_html,
        )
        return True

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = job.to
    message["Subject"] = job.subject
    if job.body_text:
        message.set_content(job.body_text)
        message.add_alternative(job.body_html, subtype="html")
    else:
        message.add_alternative(job.body_html, subtype="html")

    try:
        import aiosmtplib  # type: ignore
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_pass,
            start_tls=settings.smtp_use_tls,
        )
        return True
    except Exception as exc:
        log.warning("email.smtp_send_failed to=%s err=%s", job.to, exc)
        return False


async def _write_dead_letter(job, last_error):
    _METRICS["deadletter_count"] = int(_METRICS["deadletter_count"]) + 1
    log.error(
        "email.deadlettered to=%s event_type=%s attempts=%d error=%s",
        job.to, job.event_type, job.attempts, last_error,
    )
    try:
        from backend.db import SessionLocal
        from backend.models import EmailDeadLetter  # type: ignore
    except Exception:
        log.warning(
            "email.deadletter.fallback model_missing payload=%s", job.payload,
        )
        return
    try:
        async with SessionLocal() as session:
            row = EmailDeadLetter(
                to_addr=job.to,
                subject=job.subject,
                body_html=job.body_html,
                body_text=job.body_text,
                event_type=job.event_type,
                payload=job.payload,
                attempts=job.attempts,
                last_error=last_error,
            )
            session.add(row)
            await session.commit()
    except Exception as exc:
        log.exception("email.deadletter.db_write_failed err=%s", exc)


async def _process_job(job, email_service):
    start = time.monotonic()
    job.attempts += 1
    ok = False
    err = ""
    try:
        ok = await _send_one(job, email_service)
    except Exception as exc:
        err = repr(exc)
        log.warning(
            "email.unexpected_error to=%s event_type=%s attempt=%d err=%s",
            job.to, job.event_type, job.attempts, err,
        )

    if ok:
        latency_ms = int((time.monotonic() - start) * 1000)
        _METRICS["sent_times"].append(time.monotonic())
        log.info(
            "email.sent to=%s event_type=%s latency_ms=%d",
            job.to, job.event_type, latency_ms,
        )
        return

    _METRICS["failed_times"].append(time.monotonic())
    err = err or "smtp send returned False"
    log.warning(
        "email.failed to=%s event_type=%s attempt=%d error=%s",
        job.to, job.event_type, job.attempts, err,
    )

    if job.attempts >= _MAX_ATTEMPTS:
        await _write_dead_letter(job, err)
        return

    delay = _RETRY_DELAYS[job.attempts - 1]
    await asyncio.sleep(delay)
    _get_queue().put_nowait(job)


async def worker_loop():
    email_service = get_email_service()
    queue = _get_queue()
    log.info("email.worker.started")
    try:
        while True:
            job = await queue.get()
            try:
                await _process_job(job, email_service)
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        log.info("email.worker.stopping remaining_queue=%d", queue.qsize())
        raise


def start_worker():
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(worker_loop(), name="email-worker")
    return _worker_task


async def stop_worker(timeout: float = 2.0) -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await asyncio.wait_for(_worker_task, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    except Exception as exc:
        log.debug("email worker shutdown: %s", exc)
    _worker_task = None
